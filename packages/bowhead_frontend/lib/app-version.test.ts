import { PRODUCT_NAME } from "@/lib/product-identity";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  formatVersionDetails,
  formatVersionDetailsInline,
  loadServerVersion,
  resetServerVersionCacheForTests,
  versionLabel,
  webBuildVersion,
} from "./app-version";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  resetServerVersionCacheForTests();
});

describe("app version", () => {
  it("labels the version with the team application name, falling back to the edition name", () => {
    vi.stubEnv("NEXT_PUBLIC_BOWHEAD_VERSION", "1.4.8");
    expect(versionLabel("Example Insights")).toBe("Example Insights 1.4.8");
    expect(versionLabel("  ")).toBe(`${PRODUCT_NAME} 1.4.8`);
    vi.stubEnv("NEXT_PUBLIC_BOWHEAD_VERSION", "");
    expect(webBuildVersion()).toBe("dev");
  });

  it("keeps the edition name in the copied details of a branded team and marks a missing server version", () => {
    expect(formatVersionDetails("Example Insights", { web: "1.4.8", build: "de-DE", server: null })).toBe(
      `Example Insights 1.4.8 (${PRODUCT_NAME})\nWeb: 1.4.8 (de-DE build)\nServer: unknown`,
    );
    expect(formatVersionDetails(PRODUCT_NAME, { web: "dev", build: null, server: "1.4.8" })).toBe(
      `${PRODUCT_NAME} dev\nWeb: dev\nServer: 1.4.8`,
    );
  });

  it("gives the manual-copy prompt a single line joined with a middle dot", () => {
    expect(formatVersionDetailsInline(PRODUCT_NAME, { web: "1.4.8", build: "en-US", server: "1.4.9" })).toBe(
      `${PRODUCT_NAME} 1.4.8 · Web: 1.4.8 (en-US build) · Server: 1.4.9`,
    );
  });

  it("reads the server version once per page load and retries after a failure", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response("down", { status: 503 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ version: "1.4.9" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(loadServerVersion()).resolves.toBeNull();
    await expect(loadServerVersion()).resolves.toBe("1.4.9");
    await expect(loadServerVersion()).resolves.toBe("1.4.9");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
