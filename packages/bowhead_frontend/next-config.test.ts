import { afterEach, describe, expect, it, vi } from "vitest";
import { execFileSync } from "node:child_process";
import { copyFileSync, mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

describe("Next development proxy contract", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("retains the full Community check scope and splits only Business checks", () => {
    const directory = mkdtempSync(path.join(tmpdir(), "bowhead-next-config-"));
    try {
      mkdirSync(path.join(directory, "scripts"));
      writeFileSync(path.join(directory, "scripts/assemble-edition.mjs"), "");
      copyFileSync("next.config.mjs", path.join(directory, "next.config.mjs"));
      for (const edition of ["business", "community"]) {
        writeFileSync(path.join(directory, "build-manifest.json"), JSON.stringify({ schema_version: 1, edition }));
        const result = execFileSync(process.execPath, ["--input-type=module", "-e",
          "const {default: config} = await import(process.argv[1]); console.log(JSON.stringify(config.typescript));",
          pathToFileURL(path.join(directory, "next.config.mjs")).href,
        ], { env: { ...process.env, NEXT_STATIC_EXPORT: "1", BOWHEAD_SKIP_BUILD_CHECKS: "0" }, encoding: "utf8" });
        expect(JSON.parse(result)).toEqual({ tsconfigPath: edition === "business" ? "tsconfig.production.json" : "tsconfig.json" });
      }
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });

  it("proxies runtime health and APIs to the default local Gateway", async () => {
    vi.stubEnv("BOWHEAD_DEV_PROXY_TARGET", "");
    vi.stubEnv("NEXT_STATIC_EXPORT", "0");
    // The production config intentionally remains plain ESM JavaScript; this
    // test exercises its runtime shape rather than introducing a parallel TS copy.
    // @ts-expect-error no declaration file is emitted for next.config.mjs
    const { default: config } = await import("./next.config.mjs");

    expect(config.rewrites).toBeTypeOf("function");
    await expect(config.rewrites?.()).resolves.toEqual([
      {
        source: "/health",
        destination: "http://localhost:8080/health",
      },
      {
        source: "/api/v1/:path*",
        destination: "http://localhost:8080/api/v1/:path*",
      },
    ]);
  });

  it("uses a server-only proxy target for remote Gateway dogfood", async () => {
    vi.stubEnv("BOWHEAD_DEV_PROXY_TARGET", "http://192.0.2.10");
    vi.stubEnv("NEXT_STATIC_EXPORT", "0");
    // @ts-expect-error no declaration file is emitted for next.config.mjs
    const { default: config } = await import("./next.config.mjs");

    await expect(config.rewrites?.()).resolves.toEqual([
      {
        source: "/health",
        destination: "http://192.0.2.10/health",
      },
      {
        source: "/api/v1/:path*",
        destination: "http://192.0.2.10/api/v1/:path*",
      },
    ]);
  });
});
