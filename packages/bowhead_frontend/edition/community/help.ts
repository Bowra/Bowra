import type { HelpArticleDefinition } from "@/lib/help-content";

export const EXTRA_HELP_ARTICLES: readonly HelpArticleDefinition[] = [];
export function extraHelpRoot(_source: string): string | null {
  return null;
}
