import { readFile, stat } from "node:fs/promises";
import process from "node:process";

import { chromium } from "playwright";

const MAX_INPUT_BYTES = 4 * 1024 * 1024;
const MAX_OUTPUT_BYTES = 50 * 1024 * 1024;
const [inputPath, outputPath] = process.argv.slice(2);

if (!inputPath || !outputPath) fail("invalid_arguments");

let browser;
try {
  const inputStat = await stat(inputPath);
  if (inputStat.size <= 0 || inputStat.size > MAX_INPUT_BYTES) {
    fail("visual_input_size_invalid");
  }
  const request = JSON.parse(await readFile(inputPath, "utf8"));
  validateRequest(request);
  const baseUrl = new URL(request.frontendBaseUrl);
  if (!["http:", "https:"].includes(baseUrl.protocol)) {
    fail("frontend_origin_invalid");
  }
  const routeUrl = new URL("/internal/canvas-export/visual", baseUrl);

  browser = await chromium.launch({
    headless: true,
    args: [
      "--disable-background-networking",
      "--disable-component-update",
      "--disable-default-apps",
      "--disable-extensions",
      "--disable-sync",
      "--no-first-run",
    ],
  });
  const context = await browser.newContext({
    viewport: { width: 1600, height: 900 },
    deviceScaleFactor: 1,
    colorScheme: "light",
    locale: request.locale,
    timezoneId: request.timezone,
    reducedMotion: "reduce",
  });
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (
      url.origin === baseUrl.origin
      || url.protocol === "data:"
      || url.protocol === "blob:"
    ) {
      await route.continue();
      return;
    }
    await route.abort("blockedbyclient");
  });
  const page = await context.newPage();
  await page.addInitScript((payload) => {
    window.__BOWHEAD_EXPORT_VISUAL__ = payload;
  }, request.payload);
  await page.goto(routeUrl.toString(), {
    waitUntil: "domcontentloaded",
    timeout: request.timeoutMs,
  });
  const root = page.getByTestId("canvas-export-visual-root");
  await root.waitFor({ state: "visible", timeout: request.timeoutMs });
  await page.waitForFunction(
    () => {
      const element = document.querySelector(
        "[data-testid='canvas-export-visual-root']",
      );
      const state = element?.getAttribute("data-export-render-state");
      if (state === "error") throw new Error("visual_route_render_failed");
      return state === "ready";
    },
    undefined,
    { timeout: request.timeoutMs },
  );
  await page.evaluate(async () => {
    await document.fonts.ready;
    await new Promise((resolve) =>
      requestAnimationFrame(() => requestAnimationFrame(resolve)),
    );
  });
  const receipt = await root.evaluate((element) => ({
    blockKey: element.getAttribute("data-export-block-key"),
    rendererVersion: element.getAttribute("data-export-renderer-version"),
    visualInputSha256: element.getAttribute(
      "data-export-visual-input-sha256",
    ),
    exportSurfaceContractVersion: element.getAttribute(
      "data-export-surface-contract-version",
    ),
    presentationChecksum: element.getAttribute(
      "data-export-presentation-checksum",
    ),
  }));
  if (
    receipt.blockKey !== request.payload.blockKey
    || receipt.rendererVersion !== request.payload.rendererVersion
    || receipt.visualInputSha256 !== request.payload.visualInputSha256
    || (
      request.payload.theme?.schemaVersion === 2
      && (
        receipt.exportSurfaceContractVersion
          !== request.payload.exportSurfaceContractVersion
        || receipt.presentationChecksum
          !== (
            request.payload.sectionKind === "chart"
              ? request.payload.frozenPresentation?.presentationChecksum
              : ""
          )
      )
    )
  ) {
    fail("visual_route_receipt_mismatch");
  }
  const bounds = await root.boundingBox();
  if (
    !bounds
    || Math.round(bounds.width) !== 1600
    || Math.round(bounds.height) !== 900
  ) {
    fail("visual_route_dimensions_invalid");
  }
  await root.screenshot({
    path: outputPath,
    type: "png",
    animations: "disabled",
  });
  const outputStat = await stat(outputPath);
  if (outputStat.size <= 0 || outputStat.size > MAX_OUTPUT_BYTES) {
    fail("visual_output_size_invalid");
  }
  const outputReceipt = {
      mime: "image/png",
      rendererVersion: request.payload.rendererVersion,
      visualInputSha256: receipt.visualInputSha256,
      width: 1600,
      height: 900,
      warnings: [],
  };
  if (request.payload.theme?.schemaVersion === 2) {
    outputReceipt.exportSurfaceContractVersion =
      receipt.exportSurfaceContractVersion;
    outputReceipt.presentationChecksum = receipt.presentationChecksum;
  }
  process.stdout.write(JSON.stringify(outputReceipt));
} catch (error) {
  const message = error instanceof Error ? error.message : "visual_render_failed";
  process.stderr.write(message.slice(0, 500));
  process.exitCode = 1;
} finally {
  if (browser) await browser.close();
}

function validateRequest(request) {
  if (
    !request
    || typeof request !== "object"
    || typeof request.frontendBaseUrl !== "string"
    || !Number.isInteger(request.timeoutMs)
    || request.timeoutMs < 1_000
    || request.timeoutMs > 120_000
    || !["zh-CN", "en-US", "de-DE", "fr-FR"].includes(request.locale)
    || typeof request.timezone !== "string"
    || !request.payload
    || typeof request.payload !== "object"
  ) {
    fail("visual_request_invalid");
  }
}

function fail(code) {
  throw new Error(code);
}
