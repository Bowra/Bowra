import type { OutputBudgetDefaults } from "./llm-config-v2-api";
import type { OutboundParamRule, OutboundParams } from "./outbound-params";
import type { RuntimeParameters } from "./runtime-parameters";

export function outputCapacity(
  defaults: OutputBudgetDefaults,
  modelId: string,
  declared?: number,
  inherited?: number,
) {
  if (declared !== undefined)
    return { value: declared, source: "model" as const };
  if (inherited !== undefined)
    return { value: inherited, source: "provider" as const };
  const known = defaults.models[modelId.trim().toLowerCase()];
  return known
    ? { value: known, source: "catalog" as const }
    : { value: defaults.unknown_capacity, source: "fallback" as const };
}

/** Display only. The server compiles and fits the actual physical request. */
export function outputPolicy(
  defaults: OutputBudgetDefaults,
  capacity: number,
  capability: string,
  params: OutboundParams,
  runtime?: RuntimeParameters,
  raw?: Record<string, Record<string, unknown>> | null,
) {
  const baseline = runtime?.defaults;
  const exception = runtime?.capability_overrides?.[capability];
  const namedRule =
    exception?.outbound_params?.max_tokens ??
    baseline?.outbound_params?.max_tokens;
  const advanced = { ...baseline?.advanced, ...exception?.advanced };
  const aliases = ["max_tokens", "max_output_tokens", "max_completion_tokens"];
  const rawValue = aliases
    .map((key) => raw?.[capability]?.[key])
    .find((value) => typeof value === "number");
  const advancedValue = aliases
    .map((key) => advanced[key])
    .find((value) => typeof value === "number");
  let mode: "auto" | "fixed" | "cap" | "omit" =
    typeof rawValue === "number" ? "fixed" : "auto";
  let value: number | undefined =
    typeof rawValue === "number"
      ? rawValue
      : Math.min(capacity, defaults.automatic_ceiling);
  function apply(rule?: OutboundParamRule) {
    if (!rule || rule.mode === "follow") return;
    if (rule.mode === "omit") {
      mode = "omit";
      value = undefined;
    } else if (rule.mode === "fixed") {
      mode = "fixed";
      value = Number(rule.value);
    } else {
      mode = "cap";
      value =
        value === undefined
          ? Number(rule.value)
          : Math.min(value, Number(rule.value));
    }
  }
  apply(params.max_tokens);
  if (typeof advancedValue === "number") {
    mode = "fixed";
    value = advancedValue;
  }
  apply(namedRule);
  return { mode, value };
}
