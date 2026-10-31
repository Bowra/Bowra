/** Content-free, versioned model-maintenance facts from the ordered Turn log. */
export interface ContextActivity {
  schema_version: 1;
  operation_id: string;
  execution_id: string;
  lane: "ordinary" | "coordination";
  subagent_id: string | null;
  profile: string;
  phase: "running" | "committed" | "skipped";
  trigger: "threshold" | "capacity" | "model_change" | "manual";
  source_generation: string | null;
  generation_id: string | null;
  reason?: "no_change";
}

const identity = (value: unknown): value is string => typeof value === "string" && value.length > 0 && value.length <= 128;

export function parseContextActivity(value: Record<string, unknown>): ContextActivity | null {
  if (value.schema_version !== 1
    || typeof value.operation_id !== "string" || !/^mco_[0-9a-f]{32}$/.test(value.operation_id)
    || !identity(value.execution_id) || !identity(value.profile)
    || (value.subagent_id !== null && !identity(value.subagent_id))
    || !["ordinary", "coordination"].includes(String(value.lane))
    || !["running", "committed", "skipped"].includes(String(value.phase))
    || !["threshold", "capacity", "model_change", "manual"].includes(String(value.trigger))
    || (value.source_generation !== null && typeof value.source_generation !== "string")
    || (value.source_generation === null
      ? value.phase !== "skipped" || value.reason !== "no_change"
      : !/^mcg_[0-9a-f]{32}$/.test(value.source_generation))
    || (value.generation_id !== null && (typeof value.generation_id !== "string" || !/^mcg_[0-9a-f]{32}$/.test(value.generation_id)))
    || (value.phase === "committed") !== (value.generation_id !== null)) return null;
  // Do not retain incidental payload content, diagnostics or raw source text.
  return {
    schema_version: 1, operation_id: value.operation_id, execution_id: value.execution_id,
    lane: value.lane as ContextActivity["lane"], subagent_id: value.subagent_id as string | null,
    profile: value.profile, phase: value.phase as ContextActivity["phase"],
    trigger: value.trigger as ContextActivity["trigger"], source_generation: value.source_generation,
    generation_id: value.generation_id as string | null,
    ...(value.reason === "no_change" ? { reason: "no_change" as const } : {}),
  };
}
