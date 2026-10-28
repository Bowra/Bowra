import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import type { Page } from "@playwright/test";

const frontendRoot = resolve(__dirname, "..");
const localRequire = createRequire(resolve(frontendRoot, "package.json"));
const bundles = new Map<string, Promise<string>>();
/** Next's installed webpack bundles the real React component and its dependencies.
 * Test-only loaders transpile TS and inject its private CSS. No production test route,
 * editor/catalog hook mock, dependency addition or persistent bundle is needed.
 */
async function bundleComponent(entry: string): Promise<string> {
  const directory = await mkdtemp(resolve(tmpdir(), "reference-component-"));
  try {
    const tsLoader = resolve(directory, "typescript.cjs");
    const cssLoader = resolve(directory, "css.cjs");
    await writeFile(
      tsLoader,
      `const ts = require(${JSON.stringify(localRequire.resolve("typescript"))}); module.exports = function(source) { return ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX}}).outputText; };`,
    );
    await writeFile(
      cssLoader,
      `module.exports = function(source) {return 'const style = document.createElement("style"); style.textContent = ' + JSON.stringify(source) + '; document.head.append(style);';};`,
    );
    const nextWebpack = localRequire("next/dist/compiled/webpack/webpack");
    nextWebpack.init();
    const compiler = nextWebpack.webpack({
      mode: "development",
      target: "web",
      devtool: false,
      context: frontendRoot,
      entry: resolve(__dirname, entry),
      output: { path: directory, filename: "component.js" },
      resolve: {
        alias: { "@": frontendRoot },
        extensions: [".tsx", ".ts", ".mjs", ".js", ".json"],
        // "..." keeps webpack's per-request "import"/"require" condition, so a
        // CommonJS require() (Mammoth requiring underscore) gets the CommonJS
        // build instead of an ES module inside a "type": "commonjs" package.
        conditionNames: ["browser", "..."],
        mainFields: ["browser", "module", "main"],
      },
      module: {
        rules: [
          { test: /\.tsx?$/, exclude: /node_modules/, use: tsLoader },
          { test: /\.css$/, use: cssLoader },
        ],
      },
      plugins: [
        new nextWebpack.webpack.optimize.LimitChunkCountPlugin({ maxChunks: 1 }),
        new nextWebpack.webpack.DefinePlugin({
          "process.env.NEXT_PUBLIC_BOWHEAD_VERSION": JSON.stringify(
            "reference-component-test",
          ),
        }),
      ],
    });
    await new Promise<void>((done, fail) => {
      compiler.run(
        (
          error: Error | null,
          stats:
            { hasErrors: () => boolean; toString: () => string } | undefined,
        ) => {
          compiler.close((closeError: Error | null) => {
            if (error || closeError) fail(error || closeError);
            else if (!stats || stats.hasErrors())
              fail(new Error(stats?.toString() ?? "Missing webpack result"));
            else done();
          });
        },
      );
    });
    return await readFile(resolve(directory, "component.js"), "utf8");
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
}

export async function installReferenceComponent(
  page: Page,
  basePath = "",
  entry = "_composer-reference-component-entry.tsx",
) {
  if (!bundles.has(entry)) bundles.set(entry, bundleComponent(entry));
  const body = await bundles.get(entry)!;
  // Mirror the authenticated (app) layout: it always imports ui-controls-legacy.css (legacy classes moved from inline styles,
  // including Toast positioning and layers) and the interface stylesheets.
  const [globals, composerStyles, legacyControls, refresh, refreshSurfaces, refreshCards] = await Promise.all([
    readFile(resolve(frontendRoot, "app/globals.css"), "utf8"),
    readFile(resolve(frontendRoot, "styles/composer-layout-v2.css"), "utf8"),
    readFile(resolve(frontendRoot, "styles/ui-controls-legacy.css"), "utf8"),
    readFile(resolve(frontendRoot, "styles/ui-refresh.css"), "utf8"),
    readFile(resolve(frontendRoot, "styles/ui-refresh-surfaces.css"), "utf8"),
    // Chat cards: attachment chips, composer notices and the revision target bar.
    readFile(resolve(frontendRoot, "styles/ui-refresh-cards.css"), "utf8"),
  ]);
  await page.route("**/__reference_component.js", (route) =>
    route.fulfill({ contentType: "text/javascript", body }),
  );
  await page.route("**/__reference_component.css", (route) =>
    route.fulfill({
      contentType: "text/css",
      body: composerStyles + globals.replace(/^@import[^;]+;/gm, "") + legacyControls + refresh + refreshSurfaces + refreshCards,
    }),
  );
  await page.route(/\/__reference_component_test(?:\?.*)?$/, (route) =>
    route.fulfill({
      contentType: "text/html",
      body: `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="bowhead-base-path" content="${basePath}"><title>Reference component contract</title><link rel="stylesheet" href="${basePath}/__reference_component.css"><style>body{margin:0;font-family:system-ui;background:var(--c-bg);color:var(--c-ink)}main{padding:16px}h1{font-size:1rem}.test-composer{margin: max(240px, calc(100vh - 300px)) auto 0;max-width:720px}#before,#after{min-height:44px;padding:8px}</style></head><body><div id="root"></div><script src="${basePath}/__reference_component.js"></script></body></html>`,
    }),
  );
}
