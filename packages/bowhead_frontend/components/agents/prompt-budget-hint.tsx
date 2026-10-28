"use client";

import type { useTranslations } from "next-intl";

import {
  MAX_PROMPT_TOKENS,
  RECOMMENDED_PROMPT_TOKENS,
  estimatePromptTokens,
  promptBudgetLevel,
} from "@/lib/agent-prompt-budget";

/**
 * Budget hint below the Agent instruction editor. Replaces the character counter: Chinese and English differ sixfold in
 * tokens per character, making remaining-capacity hints misleading (see lib/agent-prompt-budget.ts).
 *
 * The two callers (team Agent release page and in-chat draft review card) have different i18n namespaces, but both include
 * `prompt_budget` / `prompt_budget_over`, so accept the caller's `t` directly.
 */
export function PromptBudgetHint({
  text,
  t,
}: {
  text: string;
  t: ReturnType<typeof useTranslations>;
}) {
  const tokens = estimatePromptTokens(text);
  const level = promptBudgetLevel(tokens, text);
  if (level === "empty") {
    // The server already rejects empty values; show the missing required input in the editor instead of waiting for a 422 on save.
    return <small data-budget="empty">{t("prompt_budget_required")}</small>;
  }
  return (
    <small data-budget={level}>
      {t("prompt_budget", { tokens, max: MAX_PROMPT_TOKENS })}
      {level === "over-recommended" || level === "over-max"
        ? ` · ${t("prompt_budget_over", { rec: RECOMMENDED_PROMPT_TOKENS })}`
        : ""}
    </small>
  );
}
