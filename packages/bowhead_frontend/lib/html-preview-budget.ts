/** Preview budgets do not change original-file storage or download limits. */
export const HTML_PREVIEW_BUDGET = {
  inputBytes: 50 * 1024 * 1024,
  outputBytes: 64 * 1024 * 1024,
  nodes: 20_000,
  depth: 128,
  handlers: 4_096,
  scriptBytes: 8 * 1024 * 1024,
  deadlineMs: 10_000,
} as const;

export class HtmlPreviewBudgetError extends Error {
  constructor() {
    super("HTML preview preparation exceeded its resource budget");
    this.name = "HtmlPreviewBudgetError";
  }
}
