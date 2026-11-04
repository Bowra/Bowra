import { buildInteractiveHtmlDocument, validateHtmlComplexity } from "./interactive-html";
import { HtmlPreviewBudgetError } from "./html-preview-budget";

// This trusted worker parses strings. It must never eval or import authored code.
self.onmessage = async (event: MessageEvent<{ raw: string; interactive: boolean }>) => {
  try {
    const result = event.data.interactive
      ? await buildInteractiveHtmlDocument(event.data.raw)
      : (validateHtmlComplexity(event.data.raw), null);
    self.postMessage({ ok: true, result });
  } catch (error) {
    self.postMessage({ ok: false, budget: error instanceof HtmlPreviewBudgetError });
  }
};
