/**
 * Pure model-level outbound-parameter logic (form ↔ persisted shape, protocol differences, conflict detection).
 *
 * Background: `docs/sprints/llm-outbound-params-2026-08/tech-design.md`. A model service's sampling-parameter
 * constraints are **its own facts**, declared in configuration and enforced on outbound requests. Design:
 * `docs/models-settings-outbound-params-design-brief-2026-08-18.md`.
 *
 * Deliberately React-free: protocol differences/conflict detection are **criteria**, enforceable in isolation.
 */

/** Four parameters in UI order (design ④B). */
export const OUTBOUND_PARAM_KEYS = [
  "temperature",
  "top_p",
  "max_tokens",
  "parallel_tool_calls",
] as const;

export type OutboundParamKey = (typeof OUTBOUND_PARAM_KEYS)[number];
export type OutboundParamMode = "follow" | "fixed" | "omit" | "cap";

export interface OutboundParamRule {
  mode: OutboundParamMode;
  value?: number | boolean;
}

export type OutboundParams = Partial<Record<OutboundParamKey, OutboundParamRule>>;

/** Only `max_tokens` supports a cap. */
const CAP_CAPABLE = new Set<OutboundParamKey>(["max_tokens"]);

/**
 * Modes available for this parameter/protocol.
 *
 * ★ Anthropic `max_tokens` is **required**; removing it makes request invalid, so omission mode never appears
 * for this protocol (backend `_validate_outbound_params` rejects at save too; same criterion at both ends).
 */
export function outboundParamModes(
  param: OutboundParamKey,
  protocol: string,
): OutboundParamMode[] {
  const modes: OutboundParamMode[] = ["follow", "fixed"];
  if (!(protocol === "anthropic_compatible" && param === "max_tokens")) {
    modes.push("omit");
  }
  if (CAP_CAPABLE.has(param)) modes.push("cap");
  return modes;
}

/** Whether this mode requires numeric input. */
export function outboundParamNeedsValue(mode: OutboundParamMode): boolean {
  return mode === "fixed" || mode === "cap";
}

/** Number of administrator-declared parameters (`follow` does not count: no declaration). */
export function declaredOutboundParamCount(
  params: OutboundParams | undefined,
): number {
  if (!params) return 0;
  return OUTBOUND_PARAM_KEYS.filter(
    (key) => (params[key]?.mode ?? "follow") !== "follow",
  ).length;
}

/**
 * Persisted shape: only **declared** parameters.
 *
 * All `follow` returns undefined, omitting key entirely: absent and all-follow configurations
 * must have identical evidence signatures (backend `_outbound_params_declaration` also discards follow).
 */
export function serializeOutboundParams(
  params: OutboundParams | undefined,
): Record<string, OutboundParamRule> | undefined {
  if (!params) return undefined;
  const out: Record<string, OutboundParamRule> = {};
  for (const key of OUTBOUND_PARAM_KEYS) {
    const rule = params[key];
    if (!rule || rule.mode === "follow") continue;
    out[key] = outboundParamNeedsValue(rule.mode)
      ? { mode: rule.mode, value: rule.value }
      : { mode: rule.mode };
  }
  return Object.keys(out).length ? out : undefined;
}

/** Server shape → form state (ignore unknown keys/invalid shapes so corrupt config never blocks editor). */
export function parseOutboundParams(raw: unknown): OutboundParams {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
  const source = raw as Record<string, unknown>;
  const out: OutboundParams = {};
  for (const key of OUTBOUND_PARAM_KEYS) {
    const rule = source[key];
    if (!rule || typeof rule !== "object" || Array.isArray(rule)) continue;
    const mode = (rule as { mode?: unknown }).mode;
    if (mode !== "fixed" && mode !== "omit" && mode !== "cap") continue;
    const value = (rule as { value?: unknown }).value;
    out[key] = outboundParamNeedsValue(mode)
      ? { mode, value: typeof value === "number" || typeof value === "boolean" ? value : undefined }
      : { mode };
  }
  return out;
}

/** Whether form can save (`fixed`/`cap` require numeric values). */
export function outboundParamsValid(params: OutboundParams | undefined, strictValues = false): boolean {
  if (!params) return true;
  return OUTBOUND_PARAM_KEYS.every((key) => {
    const rule = params[key];
    if (!rule || !outboundParamNeedsValue(rule.mode)) return true;
    if (!strictValues) return typeof rule.value === "boolean" || (typeof rule.value === "number" && Number.isFinite(rule.value));
    if (key === "parallel_tool_calls") return typeof rule.value === "boolean";
    if (typeof rule.value !== "number" || !Number.isFinite(rule.value)) return false;
    if (key === "max_tokens") return Number.isInteger(rule.value) && rule.value > 0;
    if (key === "temperature") return rule.value >= 0 && rule.value <= 2;
    return rule.value > 0 && rule.value <= 1;
  });
}

/**
 * Parameter names colliding with raw JSON escape hatch.
 *
 * Outbound order: base → overrides → declarations, so **declarations above win** on collision. Only disclose here,
 * never rewrite user JSON (design ④D: no automatic modification).
 */
export function outboundParamsConflicts(
  params: OutboundParams | undefined,
  rawOverrides: Record<string, Record<string, unknown>> | null | undefined,
): OutboundParamKey[] {
  if (!params || !rawOverrides) return [];
  const declared = new Set(
    OUTBOUND_PARAM_KEYS.filter(
      (key) => (params[key]?.mode ?? "follow") !== "follow",
    ),
  );
  const touched = new Set<string>();
  for (const perCapability of Object.values(rawOverrides)) {
    if (!perCapability || typeof perCapability !== "object") continue;
    for (const key of Object.keys(perCapability)) touched.add(key);
  }
  return OUTBOUND_PARAM_KEYS.filter(
    (key) => declared.has(key) && touched.has(key),
  );
}

/** One runtime auto-adaptation observation (`GET /api/v1/admin/llm-config/outbound-adaptations`). */
export interface OutboundAdaptation {
  provider: string;
  model_id: string;
  param: string;
  since: string;
}

/**
 * Should this row show Automatically adapted?
 *
 * ★ Human declarations outrank machine observations: runtime adaptation never intervenes for declared parameters,
 * so UI must not claim machine changes either (otherwise describes behavior runtime will not perform).
 */
export function adaptationsForModel(
  adaptations: readonly OutboundAdaptation[] | undefined,
  provider: string,
  modelId: string,
  params: OutboundParams | undefined,
): OutboundAdaptation[] {
  if (!adaptations) return [];
  return adaptations.filter(
    (item) =>
      item.provider === provider
      && item.model_id === modelId
      && (OUTBOUND_PARAM_KEYS as readonly string[]).includes(item.param)
      && (params?.[item.param as OutboundParamKey]?.mode ?? "follow") === "follow",
  );
}
