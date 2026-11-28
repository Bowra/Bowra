import type { Page } from "@playwright/test";

/**
 * The reader left Results open in these conversations (chat running state R2.7). A report no longer opens Results
 * by itself; specs that read the report pane from the first frame start from that remembered choice.
 */
export async function rememberResultsOpen(page: Page, sessionIds: readonly string[]): Promise<void> {
  await page.addInitScript((ids: readonly string[]) => {
    const key = "bowhead.results_open.v1";
    const store = JSON.parse(window.localStorage.getItem(key) ?? "{}") as Record<string, unknown>;
    for (const id of ids) store[id] = { state: "open", at: Date.now() };
    window.localStorage.setItem(key, JSON.stringify(store));
  }, sessionIds);
}
