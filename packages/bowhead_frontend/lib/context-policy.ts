export interface ContextPolicy {
  enabled?: boolean;
  trigger_input_tokens?: number | null;
  keep_recent_tokens?: number | null;
}

export function contextPolicyValid(value: ContextPolicy | undefined | null): boolean {
  if (value == null) return true;
  if (typeof value !== "object" || Array.isArray(value)) return false;
  if (Object.keys(value).some((key) => !["enabled", "trigger_input_tokens", "keep_recent_tokens"].includes(key))) return false;
  if (value.enabled !== undefined && typeof value.enabled !== "boolean") return false;
  const { trigger_input_tokens: trigger, keep_recent_tokens: recent } = value;
  if ([trigger, recent].some((number) => number != null && (!Number.isSafeInteger(number) || number <= 0))) return false;
  return trigger == null || recent == null || recent < trigger;
}
