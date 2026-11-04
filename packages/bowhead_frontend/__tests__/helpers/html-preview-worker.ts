import { vi } from "vitest";
import { buildInteractiveHtmlDocument, validateHtmlComplexity } from "@/lib/interactive-html";
import { HtmlPreviewBudgetError } from "@/lib/html-preview-budget";

/** jsdom transport double; real module-worker delivery is covered by Playwright. */
export function installHtmlPreviewWorker() {
  class PreviewWorker {
    onmessage: ((event: MessageEvent) => void) | null = null;
    onerror: (() => void) | null = null;
    onmessageerror: (() => void) | null = null;
    terminated = false;
    terminate() { this.terminated = true; }
    postMessage({ raw, interactive }: { raw: string; interactive: boolean }) {
      void (async () => {
        try {
          const result = interactive ? await buildInteractiveHtmlDocument(raw) : (validateHtmlComplexity(raw), null);
          if (!this.terminated) this.onmessage?.({ data: { ok: true, result } } as MessageEvent);
        } catch (error) {
          if (!this.terminated) this.onmessage?.({ data: { ok: false, budget: error instanceof HtmlPreviewBudgetError } } as MessageEvent);
        }
      })();
    }
  }
  vi.stubGlobal("Worker", PreviewWorker);
}
