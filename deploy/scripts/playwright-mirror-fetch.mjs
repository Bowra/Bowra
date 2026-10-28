// W1 (export-runtime-hardening tech-design §1.2): build-time browser-download mirror warm-up shim.
//
// Why it exists: 116 reaches cdn.playwright.dev at only ~25KB/s; on a cold-cache build, downloading
// Chrome Headless Shell (112MiB) alone takes ~75 minutes; the same file on npmmirror's chrome-for-testing
// mirror was measured at ~2MB/s. But npmmirror's playwright mirror has no builds/cft/ path
// (measured 404), so simply pointing PLAYWRIGHT_DOWNLOAD_HOST there does not work.
//
// Approach: this process ① runs the official installer's --dry-run to parse the exact URLs it will request
// (layout automatically follows playwright upgrades, without hardcoding versions); ② prefetches files that map to npmmirror
// locally (verify 200 + zip magic); ③ starts a 127.0.0.1 HTTP service:
// prefetch hit → serve the local file; miss → 302 to the official upstream CDN. The installer remains
// the sole authority for layout/markers — this shim only accelerates transfer, without touching /ms-playwright internals.
//
// Failure semantics (fail-open): any step's failure writes the ready marker, then exits normally/continues;
// if Dockerfile finds no port file, it does not set PLAYWRIGHT_DOWNLOAD_HOST,
// falling back to the slow official CDN — at worst the status quo, never worse.
//
// Usage: node playwright-mirror-fetch.mjs <state-dir>
//   state-dir/ready  exists = initialization complete (whether successful or not)
//   state-dir/port   exists = shim available; contains the port number

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";

const UPSTREAM = "https://cdn.playwright.dev";
const MIRROR_RULES = [
  // Chrome for Testing layout → npmmirror's chrome-for-testing mirror
  { re: /^\/builds\/cft\/(.+)$/, to: (m) => `https://cdn.npmmirror.com/binaries/chrome-for-testing/${m[1]}` },
  // Classic playwright builds layout (ffmpeg etc.) → npmmirror's playwright mirror
  { re: /^(?:\/dbazure\/download\/playwright)?\/builds\/(.+)$/, to: (m) => `https://cdn.npmmirror.com/binaries/playwright/builds/${m[1]}` },
];

const stateDir = process.argv[2];
if (!stateDir) {
  console.error("[pw-mirror] usage: playwright-mirror-fetch.mjs <state-dir>");
  process.exit(1);
}
fs.mkdirSync(stateDir, { recursive: true });
const markReady = (msg) => {
  console.log(`[pw-mirror] ${msg}`);
  fs.writeFileSync(path.join(stateDir, "ready"), "");
};

let wantedPaths = [];
try {
  const out = execFileSync(
    "node",
    ["node_modules/playwright/cli.js", "install", "--only-shell", "chromium", "--dry-run"],
    { encoding: "utf8" },
  );
  wantedPaths = [...out.matchAll(/Download url:\s+(\S+)/g)].map((m) => new URL(m[1]).pathname);
} catch (error) {
  markReady(`dry-run failed (${error.message}); shim disabled`);
  process.exit(0);
}

async function prefetch(mirrorUrl, dest) {
  // Timeout fallback: a hung mirror must not stall the build (Dockerfile already abandons the shim after waiting 120s for ready
  // and falls back to CDN; this timeout ensures this process also finishes and exits).
  const response = await fetch(mirrorUrl, {
    redirect: "follow",
    signal: AbortSignal.timeout(240_000),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const bytes = Buffer.from(await response.arrayBuffer());
  // zip magic "PK\x03\x04" — immediately fail if the mirror returns an HTML error page or similar
  if (bytes.length < 4 || bytes.readUInt32LE(0) !== 0x04034b50) throw new Error("not a zip");
  fs.writeFileSync(dest, bytes);
  return bytes.length;
}

const prefetched = new Map();
for (const wanted of wantedPaths) {
  const rule = MIRROR_RULES.find((r) => r.re.test(wanted));
  if (!rule) continue;
  const mirrorUrl = rule.to(wanted.match(rule.re));
  const dest = path.join(stateDir, Buffer.from(wanted).toString("hex").slice(0, 40) + ".zip");
  console.log(`[pw-mirror] prefetching ${wanted} <- ${mirrorUrl}`);
  const startedAt = Date.now();
  try {
    const size = await prefetch(mirrorUrl, dest);
    prefetched.set(wanted, dest);
    console.log(`[pw-mirror] prefetched ${(size / 1048576).toFixed(1)} MiB in ${((Date.now() - startedAt) / 1000).toFixed(0)}s`);
  } catch (error) {
    console.log(`[pw-mirror] mirror miss for ${wanted} (${error.message}); will redirect to upstream`);
  }
}

if (prefetched.size === 0) {
  markReady("nothing prefetched; shim disabled, installer will use default CDN");
  process.exit(0);
}

const server = http.createServer((request, response) => {
  const pathname = request.url.split("?")[0];
  const local = prefetched.get(pathname);
  if (local) {
    response.writeHead(200, {
      "content-type": "application/zip",
      "content-length": fs.statSync(local).size,
    });
    fs.createReadStream(local).pipe(response);
    return;
  }
  response.writeHead(302, { location: UPSTREAM + request.url });
  response.end();
});
server.listen(0, "127.0.0.1", () => {
  fs.writeFileSync(path.join(stateDir, "port"), String(server.address().port));
  markReady(`serving ${prefetched.size} prefetched file(s) on 127.0.0.1:${server.address().port}`);
});
