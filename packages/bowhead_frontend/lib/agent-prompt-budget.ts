/**
 * Agent-instruction token budget—live editor hints.
 *
 * Why not characters: equal character counts differ sixfold in tokens by language (cl100k measurements: pure Chinese
 * 1.05 tok/char, mixed Chinese/English 0.63, pure English 0.17); character counters falsely reassure Chinese authors
 * and make English authors think space is exhausted. Server gate (`subagent_prompt_limits.MAX_PROMPT_TOKENS`)
 * already uses tokens; editor must show the same criterion.
 *
 * Coefficients fit real corpus by least squares: 8 built-in Agent prompts + 37 built-in SKILL.md +
 * Anthropic claude-api SKILL.md (n=46), fit 1.386/0.249 rounded to 1.4/0.25,
 * median relative deviation −0.7%, range −14% ~ +28%.
 *
 * ⚠ This is an **estimate**; server verdict is authoritative, so buttons use it only for hints, never blocking.
 */

const CJK = /[　-〿㐀-鿿豈-﫿＀-￯]/;

/** Server `MAX_PROMPT_TOKENS`—exceeding it is always rejected. */
export const MAX_PROMPT_TOKENS = 16000;
/**
 * Server `MAX_PROMPT_CHARS_TEAM`—hard textarea `maxLength` boundary.
 * Only a pre-tokenization check; the token threshold above is the actual over-budget criterion.
 */
export const MAX_PROMPT_CHARS_TEAM = 24000;
/** Recommended threshold: round up our largest persistent Agent prompt (5145 tok after layered compute rewrite). */
export const RECOMMENDED_PROMPT_TOKENS = 6000;

export function estimatePromptTokens(text: string): number {
  let cjk = 0;
  let total = 0;
  // Count code points, not `text.length` (UTF-16 units)—Python counted code points for fitting;
  // surrogate pairs such as emoji otherwise make the two criteria diverge.
  for (const ch of text) {
    total += 1;
    if (CJK.test(ch)) cjk += 1;
  }
  return Math.round(cjk * 1.4 + (total - cjk) * 0.25);
}

export type PromptBudgetLevel = "empty" | "ok" | "over-recommended" | "over-max";

/**
 * `empty` means **required but missing**, not ample capacity. Server already rejects empty
 * (`subagent_system_prompt_required`)—116 once created 3 Agents with zero instructions,
 * one actually dispatched for execution, so this tier must be conspicuous.
 */
export function promptBudgetLevel(tokens: number, text = ""): PromptBudgetLevel {
  if (!text.trim()) return "empty";
  if (tokens > MAX_PROMPT_TOKENS) return "over-max";
  if (tokens > RECOMMENDED_PROMPT_TOKENS) return "over-recommended";
  return "ok";
}
