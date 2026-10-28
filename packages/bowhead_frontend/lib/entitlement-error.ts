import type { Locale } from "./locale";
import { staticTranslator } from "./locale-messages";

type LicensedFeature =
  | "mcp"
  | "kb"
  | "scheduling"
  | "audit_export"
  | "custom_branding";

type EntitlementError = {
  code?: unknown;
  body?: unknown;
};

const FEATURES = new Set<LicensedFeature>([
  "mcp",
  "kb",
  "scheduling",
  "audit_export",
  "custom_branding",
]);

/** Finite entitlement codes with copy in messages `entitlement_error.codes`. */
const ENTITLEMENT_CODES = new Set<string>([
  "entitlement_unavailable",
  "needs_license",
  "over_grace",
  "suspended",
  "tamper",
  "clock_tamper",
  "seat_exceeded",
  "user_credit_exhausted",
  "team_pool_exhausted",
  "feature_disabled",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function featureFromBody(body: unknown): LicensedFeature | undefined {
  if (!isRecord(body) || !isRecord(body.detail) || !isRecord(body.detail.error)) {
    return undefined;
  }
  const details = body.detail.error.details;
  if (!isRecord(details) || typeof details.feature !== "string") return undefined;
  return FEATURES.has(details.feature as LicensedFeature)
    ? details.feature as LicensedFeature
    : undefined;
}

/**
 * Maps finite entitlement codes to safe, actionable, localized copy. Backend
 * prose is deliberately ignored; `details.feature` is a finite API contract.
 */
export function entitlementErrorMessage(
  error: EntitlementError,
  locale: Locale,
  options: { featureFallback?: LicensedFeature } = {},
): string | null {
  if (typeof error.code !== "string") return null;
  const t = staticTranslator("entitlement_error", locale);
  if (error.code === "feature_unlicensed") {
    const feature = featureFromBody(error.body) ?? options.featureFallback;
    return feature
      ? t("feature_unlicensed_named", { name: t(`features.${feature}`) })
      : t("feature_unlicensed");
  }
  return ENTITLEMENT_CODES.has(error.code) ? t(`codes.${error.code}`) : null;
}
