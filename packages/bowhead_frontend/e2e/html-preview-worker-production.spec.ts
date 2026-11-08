import { test, expect } from "@playwright/test";
import { createServer, type Server } from "node:http";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import path from "node:path";

const root = path.resolve(__dirname, "..");
const require = createRequire(path.join(root, "package.json"));
let directory: string;
let server: Server;
let origin: string;
let prefix = "";
let entryFiles: string[] = [];
const requests: string[] = [];

test.beforeAll(async () => {
  directory = await mkdtemp(path.join(tmpdir(), "bowhead-html-worker-"));
  await writeFile(
    path.join(directory, "entry.js"),
    `
    import { prepareHtmlInWorker } from ${JSON.stringify(path.join(root, "lib/html-preview-client.ts"))};
    __webpack_public_path__ = window.__BOWHEAD_BASE_PATH__ + '/_next/';
    window.prepareHtml = (raw, interactive) => prepareHtmlInWorker(raw, interactive, new AbortController().signal);
  `,
  );
  // Use Next's production compiler and SWC loader on the real client/worker.
  // Splitting the parser dependencies reproduces the independent worker runtime.
  const bundled = require("next/dist/compiled/webpack/webpack");
  bundled.init();
  const compiler = bundled.webpack({
    mode: "production",
    context: root,
    entry: path.join(directory, "entry.js"),
    target: "web",
    output: { path: directory, filename: "static/chunks/[name].js", chunkFilename: "static/chunks/[id].[contenthash].js", publicPath: "/_next/" },
    resolve: { extensions: [".ts", ".js"], modules: ["node_modules", path.join(root, "node_modules")] },
    module: {
      rules: [
        {
          test: /\.ts$/,
          exclude: /node_modules/,
          use: {
            loader: require.resolve("next/dist/build/webpack/loaders/next-swc-loader"),
            options: { rootDir: root, isServer: false, nextConfig: { experimental: {} } },
          },
        },
      ],
    },
    optimization: { minimize: false, splitChunks: { chunks: "all", minSize: 0, cacheGroups: { default: false } } },
  });
  try {
    await new Promise<void>((resolve, reject) =>
      compiler.run(
        (
          error: Error | null,
          stats: {
            hasErrors(): boolean;
            toString(): string;
            compilation: { entrypoints: Map<string, { getFiles(): string[] }> };
          },
        ) => {
          if (error) reject(error);
          else if (stats.hasErrors()) reject(new Error(stats.toString()));
          else {
            entryFiles = stats.compilation.entrypoints.get("main")!.getFiles();
            resolve();
          }
        },
      ),
    );
  } finally {
    await new Promise<void>((resolve, reject) => compiler.close((error: Error | null) => (error ? reject(error) : resolve())));
  }
  server = createServer(async (request, response) => {
    const pathname = new URL(request.url!, "http://localhost").pathname;
    requests.push(pathname);
    if (pathname === `${prefix}/`) {
      response.setHeader("Content-Type", "text/html");
      response.end(
        `<script>window.__BOWHEAD_BASE_PATH__=${JSON.stringify(prefix)}</script>${entryFiles.map((file) => `<script src="${prefix}/_next/${file}"></script>`).join("")}`,
      );
      return;
    }
    const assetPrefix = `${prefix}/_next/`;
    if (pathname.startsWith(assetPrefix)) {
      try {
        const asset = await readFile(path.join(directory, pathname.slice(assetPrefix.length)));
        response.setHeader("Content-Type", "text/javascript");
        response.end(asset);
        return;
      } catch {
        /* Missing assets must fail, as on a proxy that strips the prefix. */
      }
    }
    response.writeHead(404).end();
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Expected an HTTP server address");
  origin = `http://127.0.0.1:${address.port}`;
});

test.afterAll(async () => {
  if (server?.listening) {
    server.closeAllConnections();
    await new Promise<void>((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
  }
  if (directory) await rm(directory, { recursive: true, force: true });
});

for (const entryPrefix of ["", "/bowhead", "/customer-portal", "/apps/team-a/assistant", "/_next/tenant"]) {
  test(`production HTML Worker preserves entry prefix ${JSON.stringify(entryPrefix)}`, async ({ page }) => {
    prefix = entryPrefix;
    requests.length = 0;
    await page.goto(`${origin}${prefix}/`);
    const raw = "<button onclick=\"this.textContent='Ready'\">Run</button>";
    const result = await page.evaluate(async (source) => {
      const prepare = (window as unknown as { prepareHtml(raw: string, interactive: boolean): Promise<{ document: string } | null> }).prepareHtml;
      return { interactive: await prepare(source, true), reading: await prepare(source, false) };
    }, raw);
    expect(result.interactive?.document).toContain("Ready");
    expect(result.reading).toBeNull();
    // Entry, worker, and split dependencies must all stay below this entry.
    const chunks = requests.filter((url) => url.endsWith(".js"));
    expect(new Set(chunks).size).toBeGreaterThanOrEqual(3);
    expect(chunks.every((url) => url.startsWith(`${prefix}/_next/`))).toBe(true);
  });
}
