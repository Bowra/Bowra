import { outboundParamsValid, type OutboundParams } from "./outbound-params";

export interface RuntimeParameterPolicy {
  thinking_mode?: "off" | "on" | "adaptive" | "omit";
  reasoning_effort?: string;
  thinking_budget?: number;
  outbound_params?: OutboundParams;
  advanced?: Record<string, unknown>;
}

export interface RuntimeParameters {
  profile?: string;
  defaults?: RuntimeParameterPolicy;
  capability_overrides?: Record<string, RuntimeParameterPolicy>;
}

export interface ParameterProfile {
  id: string;
  label: string;
  family: "openai" | "openai_responses" | "anthropic";
  provider_types: string[];
  thinking_modes: Array<NonNullable<RuntimeParameterPolicy["thinking_mode"]>>;
  efforts: string[];
  budget_min?: number;
  budget_max?: number;
  temperature_max: number;
  source: string;
}

export interface ParameterPreview {
  capability: string;
  profile?: string;
  synthetic: true;
  provider_request_count: 0;
  configuration_writes: 0;
  parameters: Array<{
    field: string;
    value?: unknown;
    omitted?: boolean;
    source: string;
    variable?: boolean;
    redacted?: boolean;
    acceptance?: string;
  }>;
  transport?: { adapter: string; api_surface: string };
}

export function updateParameterScope(
  runtime: RuntimeParameters | undefined,
  scope: string,
  policy: RuntimeParameterPolicy,
): RuntimeParameters | undefined {
  const next: RuntimeParameters = {
    ...runtime,
    capability_overrides: { ...runtime?.capability_overrides },
  };
  const cleaned = Object.fromEntries(
    Object.entries(policy).filter(
      ([, value]) =>
        value !== undefined &&
        (typeof value !== "object" || Object.keys(value).length > 0),
    ),
  );
  if (scope === "defaults") next.defaults = cleaned;
  else if (Object.keys(cleaned).length)
    next.capability_overrides![scope] = cleaned;
  else delete next.capability_overrides![scope];
  if (!Object.keys(next.capability_overrides!).length)
    delete next.capability_overrides;
  if (!Object.keys(next.defaults || {}).length) delete next.defaults;
  return next.profile || next.defaults || next.capability_overrides
    ? next
    : undefined;
}

/** Reject duplicate top-level names before JSON.parse collapses them. */
export function parseAdvancedJson(
  text: string,
): Record<string, unknown> | null {
  try {
    const object: unknown = JSON.parse(text, (_key: string, value: unknown) => {
      if (typeof value === "number" && !Number.isFinite(value)) {
        throw new Error("Advanced parameters require finite JSON numbers");
      }
      return value;
    });
    if (!object || typeof object !== "object" || Array.isArray(object))
      return null;
    let depth = 0;
    const keys = new Set<string>();
    for (let index = 0; index < text.length; index += 1) {
      const char = text[index];
      if (char === '"') {
        const start = index;
        index += 1;
        while (index < text.length) {
          if (text[index] === "\\") index += 2;
          else if (text[index] === '"') break;
          else index += 1;
        }
        if (
          depth === 1 &&
          text
            .slice(index + 1)
            .trimStart()
            .startsWith(":")
        ) {
          const key: string = JSON.parse(text.slice(start, index + 1));
          if (keys.has(key)) return null;
          keys.add(key);
        }
      } else if (char === "{" || char === "[") depth += 1;
      else if (char === "}" || char === "]") depth -= 1;
    }
    return object as Record<string, unknown>;
  } catch {
    return null;
  }
}

export function runtimeParametersValid(
  runtime: RuntimeParameters | undefined,
): boolean {
  if (
    !runtime?.profile &&
    [
      runtime?.defaults,
      ...Object.values(runtime?.capability_overrides || {}),
    ].some(
      (policy) =>
        policy &&
        (policy.thinking_mode !== undefined ||
          policy.reasoning_effort !== undefined ||
          policy.thinking_budget !== undefined),
    )
  )
    return false;
  return [
    runtime?.defaults,
    ...Object.values(runtime?.capability_overrides || {}),
  ].every(
    (policy) =>
      !policy ||
      (outboundParamsValid(policy.outbound_params, true) &&
        (policy.thinking_budget === undefined ||
          (Number.isInteger(policy.thinking_budget) &&
            policy.thinking_budget > 0)) &&
        (!(policy.thinking_mode === "off" || policy.thinking_mode === "omit") ||
          (policy.thinking_budget === undefined &&
            (!policy.reasoning_effort || policy.reasoning_effort === "omit")))),
  );
}

/** Stage conversion at its existing capability scope; never promote Chat raw fields. */
export function stageLegacyConversion(
  runtime: RuntimeParameters | undefined,
  raw: Record<string, Record<string, unknown>>,
  baseline: OutboundParams,
  profile: ParameterProfile | undefined,
) {
  let next = runtime;
  const remaining = structuredClone(raw);
  const converted: string[] = [];
  for (const [capability, values] of Object.entries(raw)) {
    if (!["chat", "tool", "vision"].includes(capability)) continue;
    let policy = { ...next?.capability_overrides?.[capability] };
    for (const [key, value] of Object.entries(values)) {
      if (
        ["temperature", "top_p", "max_tokens", "parallel_tool_calls"].includes(
          key,
        )
      ) {
        const param = key as keyof OutboundParams;
        if (
          typeof value !==
          (key === "parallel_tool_calls" ? "boolean" : "number")
        )
          continue;
        const rule = baseline[param];
        policy.outbound_params = {
          ...policy.outbound_params,
          [key]:
            policy.outbound_params?.[param] ||
            (rule && rule.mode !== "follow" ? rule : { mode: "fixed", value }),
        };
      } else if (
        key === "enable_thinking" &&
        typeof value === "boolean" &&
        profile?.family === "openai" &&
        profile.thinking_modes.includes(value ? "on" : "off")
      ) {
        policy.thinking_mode = value ? "on" : "off";
      } else if (
        key === "thinking_budget" &&
        Number.isInteger(value) &&
        profile?.family === "openai" &&
        profile.budget_min !== undefined &&
        typeof value === "number" &&
        value >= profile.budget_min &&
        value <= (profile.budget_max || 0) &&
        (values.enable_thinking === true || policy.thinking_mode === "on")
      ) {
        policy.thinking_budget = value;
      } else continue;
      delete remaining[capability][key];
      converted.push(`${capability}.${key}`);
    }
    next = updateParameterScope(next, capability, policy);
    if (!Object.keys(remaining[capability]).length)
      delete remaining[capability];
  }
  return { runtime: next, raw: remaining, converted };
}
