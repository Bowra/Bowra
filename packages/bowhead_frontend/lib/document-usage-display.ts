import type { TurnSummary } from "@/lib/types";

/** Display projection only; root counters retain their billing contract. */
export function projectDisplayUsage(payload: Record<string, unknown>): Pick<TurnSummary, "tokensInput" | "tokensOutput" | "usagePartial"> {
  const usage = payload.display_usage;
  if (usage && typeof usage === "object" && !Array.isArray(usage)) {
    const fields = usage as Record<string, unknown>;
    if (Number.isSafeInteger(fields.tokens_input) && Number.isSafeInteger(fields.tokens_output)
      && (fields.tokens_input as number) >= 0 && (fields.tokens_output as number) >= 0
      && typeof fields.partial === "boolean") {
      return { tokensInput: fields.tokens_input as number, tokensOutput: fields.tokens_output as number, usagePartial: fields.partial };
    }
  }
  return { tokensInput: payload.tokens_input as number | undefined, tokensOutput: payload.tokens_output as number | undefined };
}
