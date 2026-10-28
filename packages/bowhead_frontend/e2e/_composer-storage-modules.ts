import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { transpileModule, ModuleKind, ScriptTarget } from "typescript";
import type { Page } from "@playwright/test";

export const STORAGE_MODULE_PATH = "/__composer_storage_modules";

/** Run the production storage modules against real browser IndexedDB.
 * Only the unused network boundary is replaced; no store/CAS logic is mocked.
 */
export async function installComposerStorageModules(page: Page): Promise<void> {
  await page.route("**/__composer_storage_test", (route) =>
    route.fulfill({
      contentType: "text/html",
      body: "<!doctype html><title>Composer storage contract</title>",
    }),
  );
  const modules = new Set([
    "composer-local-persistence-db",
    "composer-document-revision",
    "composer-draft-format",
    "composer-input-document",
    "composer-attachment-refs",
    "composer-ordinary-draft",
    "composer-persistence-scope",
    "composer-chat-action",
    "composer-submission-recovery",
    "coach-canvas-target",
    "composer-transient-draft",
    "composer-reference-transfer",
    "composer-reference-handoff",
    "random-id",
    "sha256-fallback",
    "base-path",
  ]);
  await page.route(`**${STORAGE_MODULE_PATH}/*`, async (route) => {
    const name = new URL(route.request().url()).pathname.split("/").at(-1)!;
    if (name === "api-client") {
      await route.fulfill({
        contentType: "text/javascript",
        body: 'export function apiFetch() { throw new Error("Unexpected network call"); } export const readApiError = apiFetch;',
      });
      return;
    }
    if (!modules.has(name)) throw new Error(`Unexpected storage module: ${name}`);
    const source = await readFile(resolve(__dirname, `../lib/${name}.ts`), "utf8");
    const { outputText } = transpileModule(source, {
      compilerOptions: { module: ModuleKind.ESNext, target: ScriptTarget.ES2022 },
    });
    await route.fulfill({ contentType: "text/javascript", body: outputText });
  });
}
