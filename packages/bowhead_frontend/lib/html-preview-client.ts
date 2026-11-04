import { HTML_PREVIEW_BUDGET, HtmlPreviewBudgetError } from "./html-preview-budget";
import type { InteractiveHtmlDocument } from "./html-preview-contract";

let activePreparations = 0;
const queuedPreparations: Array<() => void> = [];

/** Share two preparation slots across report sections and file viewers. */
export function prepareHtmlInWorker(raw: string, interactive: boolean, signal: AbortSignal): Promise<InteractiveHtmlDocument | null> {
  return new Promise((resolve, reject) => {
    const cancel = () => {
      const index = queuedPreparations.indexOf(start);
      if (index >= 0) queuedPreparations.splice(index, 1);
      reject(new DOMException("HTML preparation cancelled", "AbortError"));
    };
    const release = () => { activePreparations--; queuedPreparations.shift()?.(); };
    const start = () => {
      signal.removeEventListener("abort", cancel);
      activePreparations++;
      runPreparation(raw, interactive, signal).then(
        (result) => { release(); resolve(result); },
        (error: unknown) => { release(); reject(error); },
      );
    };
    if (signal.aborted) { cancel(); return; }
    if (activePreparations < 2) start();
    else { queuedPreparations.push(start); signal.addEventListener("abort", cancel, { once: true }); }
  });
}

/** One cancellable worker per active preparation, with no synchronous fallback. */
function runPreparation(raw: string, interactive: boolean, signal: AbortSignal): Promise<InteractiveHtmlDocument | null> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(new DOMException("HTML preparation cancelled", "AbortError")); return; }
    const worker = new Worker(new URL("./html-preview.worker.ts", import.meta.url), { type: "module" });
    let settled = false;
    const finish = (error?: Error, result: InteractiveHtmlDocument | null = null) => {
      if (settled) return;
      settled = true;
      clearTimeout(deadline);
      signal.removeEventListener("abort", abort);
      worker.terminate();
      if (error) reject(error); else resolve(result);
    };
    const abort = () => finish(new DOMException("HTML preparation cancelled", "AbortError"));
    const deadline = setTimeout(() => finish(new HtmlPreviewBudgetError()), HTML_PREVIEW_BUDGET.deadlineMs);
    signal.addEventListener("abort", abort, { once: true });
    worker.onerror = () => finish(new Error("HTML preparation worker failed"));
    worker.onmessageerror = () => finish(new Error("HTML preparation worker returned invalid data"));
    worker.onmessage = (event: MessageEvent) => {
      if (event.data?.ok === true) finish(undefined, event.data.result);
      else finish(event.data?.budget ? new HtmlPreviewBudgetError() : new Error("HTML preparation failed"));
    };
    try { worker.postMessage({ raw, interactive }); } catch { finish(new Error("HTML preparation could not start")); }
  });
}
