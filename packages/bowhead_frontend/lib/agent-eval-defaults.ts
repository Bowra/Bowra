import type { AgentEvalCase } from "@/lib/agents-api";

/**
 * Owner cases saved before 2026-10-15 defaulted to a zero Tool-call cap, which fails an Agent for
 * querying data, and a critical `no_fabrication` check, which is now advisory and decides nothing.
 * Detection mirrors the server's `case_on_old_defaults` (the Tool-call cap only); the update removes
 * exactly those two settings, nothing else.
 */
export function scorerConfigOnOldDefaults(config: Record<string, unknown>): boolean {
  return config.max_tool_calls === 0;
}

export function withCurrentScorerDefaults(config: Record<string, unknown>): Record<string, unknown> {
  const next: Record<string, unknown> = { ...config };
  if (next.max_tool_calls === 0) delete next.max_tool_calls;
  if (Array.isArray(next.critical)) {
    const critical = next.critical.filter((name) => name !== "no_fabrication");
    if (critical.length) next.critical = critical;
    else delete next.critical;
  }
  return next;
}

/** The save body for a saved case moved to the current defaults; its task and contract are unchanged. */
export function caseWithCurrentDefaults(value: AgentEvalCase): AgentEvalCase & { task: string } {
  return {
    case_key: value.case_key,
    task: value.task ?? value.input?.task ?? "",
    fixture_ref: value.fixture_ref ?? {},
    expected_contract: value.expected_contract,
    scorer_config: withCurrentScorerDefaults(value.scorer_config),
    effect_policy: value.effect_policy,
    valid_until: value.valid_until ?? null,
  };
}
