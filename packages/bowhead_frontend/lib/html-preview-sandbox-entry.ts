/**
 * Visual self-check, slice B: the product preview's HTML transform as one browser script.
 *
 * The application sends this script with each model-written HTML file to the sandbox
 * document worker, which runs it in its network-less Chromium and shows the result in the
 * same sandboxed frame as `SafeHtmlFrame` (docs/sprints/visual-self-check-2026-11-16/
 * tech-design.md §6). Built by `npm run build:html-preview-transform`; never served.
 */
import { HtmlPreviewBudgetError } from "./html-preview-budget";
import { INTERACTIVE_HTML_PERMISSIONS, INTERACTIVE_HTML_SANDBOX } from "./html-preview-contract";
import { buildInteractiveHtmlDocument } from "./interactive-html";

(globalThis as unknown as { bowheadHtmlPreview: unknown }).bowheadHtmlPreview = {
  version: "html-preview-transform-v1",
  sandbox: INTERACTIVE_HTML_SANDBOX,
  permissions: INTERACTIVE_HTML_PERMISSIONS,
  async build(raw: string) {
    try {
      return { ok: true, ...(await buildInteractiveHtmlDocument(raw)) };
    } catch (error) {
      return { ok: false, error: error instanceof HtmlPreviewBudgetError ? "resource_limited" : "preparation_failed" };
    }
  },
};
