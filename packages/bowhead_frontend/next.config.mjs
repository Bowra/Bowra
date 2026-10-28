import "./scripts/assemble-edition.mjs";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

/** @type {import('next').NextConfig} */
// ⚠ Two modes: dev vs prod
//   - Default (dev): rewrites() proxies /api/v1/* to Gateway, supporting hot reload
//   - With `NEXT_STATIC_EXPORT=1`: output=export generates static files in ./out/; rewrites are unavailable
//     (No rewrite needed when FastAPI serves the same origin)
const isStaticExport = process.env.NEXT_STATIC_EXPORT === "1";
// Lint and type results do not depend on NEXT_PUBLIC_BUILD_LOCALE, so the image
// build checks once (en-US) and sets this for the remaining locale builds.
const skipBuildChecks = process.env.BOWHEAD_SKIP_BUILD_CHECKS === "1";
const packageRoot = dirname(fileURLToPath(import.meta.url));
const { edition } = JSON.parse(readFileSync(join(packageRoot, "build-manifest.json"), "utf8"));

const nextConfig = {
  reactStrictMode: true,
  // Gzip in Next's rewrite proxy buffers SSE until completion. Production
  // static exports use the Gateway's separate static/API compression policy.
  compress: false,
  typescript: {
    // Only the Business image has the independent test-check stage. Community
    // keeps the complete scope until its separate build recipe is reviewed.
    tsconfigPath: edition === "business" ? "tsconfig.production.json" : "tsconfig.json",
    ...(skipBuildChecks ? { ignoreBuildErrors: true } : {}),
  },
  ...(skipBuildChecks
    ? {
        eslint: { ignoreDuringBuilds: true },
      }
    : {}),
  // Keep static exports practical on self-hosted build machines.
  experimental: { cpus: 2 },
  // This package lives inside a multi-lockfile repo. Make the trace root explicit so
  // Next.js does not infer the monorepo root from unrelated lockfiles.
  outputFileTracingRoot: packageRoot,
  ...(isStaticExport
    ? {
        output: "export",
        trailingSlash: true,          // FastAPI can serve static directory indexes directly
        images: { unoptimized: true }, // Static export cannot use the next/image optimizer
      }
    : {
        async rewrites() {
          const apiBase =
            process.env.BOWHEAD_DEV_PROXY_TARGET ||
            "http://localhost:8080";
          return [
            {
              // Runtime capability hooks intentionally use the same-origin
              // operational endpoint. In dev/SSE mode Next owns that origin,
              // so proxy health alongside /api/v1 or every capability fails
              // closed against Next's 404 while the real Gateway is healthy.
              source: "/health",
              destination: `${apiBase}/health`,
            },
            {
              source: "/api/v1/:path*",
              destination: `${apiBase}/api/v1/:path*`,
            },
          ];
        },
      }),
};

// i18n is entirely client-side (components/client-locale-provider.tsx),
// so next-intl/plugin is unnecessary; export nextConfig directly.
// This lets NEXT_STATIC_EXPORT=1 prod static export build normally (cookies() is no longer called).
export default nextConfig;
