import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  window.__BOWHEAD_PUBLIC_CONTEXT__ = undefined;
  window.__BOWHEAD_BASE_PATH__ = undefined;
  window.__BOWHEAD_BASE_URL__ = undefined;
  document.querySelector('meta[name="bowhead-base-path"]')?.remove();
  document.querySelector('meta[name="bowhead-internal-path"]')?.remove();
  window.history.replaceState(null, "", "/");
});

describe("base-path", () => {
  it.each(["/bowhead", "/ai/apps/bowhead"])("keeps the %s application root canonical with session queries and fragments", async (prefix) => {
    vi.resetModules();
    window.__BOWHEAD_BASE_PATH__ = prefix;
    const { appPath, unprefixAppPath } = await import("./base-path");
    for (const suffix of ["", "?session=s_refresh&next=a%2Fb", "#results", "?session=s_refresh#results"]) {
      expect(appPath(`${prefix}${suffix}`)).toBe(`${prefix}/${suffix}`);
      expect(appPath(`${prefix}/${suffix}`)).toBe(`${prefix}/${suffix}`);
      expect(unprefixAppPath(`${prefix}${suffix}`)).toBe(`/${suffix}`);
    }
    expect(appPath(`${prefix}/sessions/`)).toBe(`${prefix}/sessions/`);
  });

  it("keeps root-path deployments unchanged", async () => {
    vi.resetModules();
    window.__BOWHEAD_BASE_PATH__ = "";
    const { appPath, unprefixAppPath } = await import("./base-path");
    expect(appPath("/api/v1/auth/me")).toBe("/api/v1/auth/me");
    expect(unprefixAppPath("/login")).toBe("/login");
  });

  it("normalizes a trailing slash and prefixes paths once", async () => {
    vi.resetModules();
    window.__BOWHEAD_BASE_PATH__ = "/bowhead/";
    const { appPath, basePath, unprefixAppPath } = await import("./base-path");
    expect(basePath()).toBe("/bowhead");
    expect(appPath("/")).toBe("/bowhead/");
    expect(appPath("/api/v1/auth/me")).toBe("/bowhead/api/v1/auth/me");
    expect(appPath("/bowhead/api/v1/auth/me")).toBe("/bowhead/api/v1/auth/me");
    expect(unprefixAppPath("/bowhead/login?next=x")).toBe("/login?next=x");
  });

  it("uses the runtime HTML metadata when the inline script did not run", async () => {
    vi.resetModules();
    window.__BOWHEAD_BASE_PATH__ = undefined;
    document.head.insertAdjacentHTML(
      "beforeend",
      '<meta name="bowhead-base-path" content="/bowhead">',
    );

    const { appPath, basePath } = await import("./base-path");
    expect(basePath()).toBe("/bowhead");
    expect(appPath("/login?next=%2Fbowhead%2F")).toBe(
      "/bowhead/login?next=%2Fbowhead%2F",
    );

    document.querySelector('meta[name="bowhead-base-path"]')?.remove();
  });

  it("prefers the verified structured public context", async () => {
    vi.resetModules();
    window.__BOWHEAD_PUBLIC_CONTEXT__ = {
      entryId: "pe_example",
      teamId: "team_example",
      baseUrl: "https://public.example/apps/bowhead",
      basePath: "/apps/bowhead",
      matchSource: "entry_id",
    };
    window.__BOWHEAD_BASE_PATH__ = "/legacy";
    window.__BOWHEAD_BASE_URL__ = "https://legacy.example/legacy";

    const { appPath, basePath, publicBaseUrl } = await import("./base-path");
    expect(basePath()).toBe("/apps/bowhead");
    expect(publicBaseUrl()).toBe(`${window.location.origin}/apps/bowhead`);
    expect(appPath("/login/")).toBe("/apps/bowhead/login/");
  });

  it("keeps the browser origin when the injected address contains an upstream port", async () => {
    vi.resetModules();
    window.__BOWHEAD_PUBLIC_CONTEXT__ = {
      entryId: null,
      teamId: null,
      baseUrl: "http://internal.example:8000/bowhead",
      basePath: "/bowhead",
      matchSource: "direct",
    };
    const { publicBaseUrl } = await import("./base-path");
    expect(publicBaseUrl()).toBe(`${window.location.origin}/bowhead`);
  });

  it("derives the current entry from the browser path and internal route", async () => {
    vi.resetModules();
    window.history.replaceState(null, "", "/bowhead/login/");
    document.head.insertAdjacentHTML(
      "beforeend",
      '<meta name="bowhead-internal-path" content="/login/">',
    );

    const { appPath, basePath, publicBaseUrl } = await import("./base-path");
    expect(basePath()).toBe("/bowhead");
    expect(publicBaseUrl()).toBe(`${window.location.origin}/bowhead`);
    expect(appPath("/api/v1/auth/me")).toBe("/bowhead/api/v1/auth/me");
  });

  it("uses the browser entry when an injected URL is stale", async () => {
    vi.resetModules();
    window.history.replaceState(null, "", "/current/login/");
    document.head.insertAdjacentHTML(
      "beforeend",
      '<meta name="bowhead-internal-path" content="/login/">',
    );
    window.__BOWHEAD_PUBLIC_CONTEXT__ = {
      entryId: "pe_stale",
      teamId: "team_example",
      baseUrl: "https://stale.example/old",
      basePath: "/old",
      matchSource: "entry_id",
    };

    const { basePath, publicBaseUrl } = await import("./base-path");
    expect(basePath()).toBe("/current");
    expect(publicBaseUrl()).toBe(`${window.location.origin}/current`);
  });

  it("keeps the root entry stable after client-side navigation", async () => {
    vi.resetModules();
    window.history.replaceState(null, "", "/");
    document.head.insertAdjacentHTML(
      "beforeend",
      '<meta name="bowhead-internal-path" content="/">',
    );

    const { appPath, basePath, publicBaseUrl } = await import("./base-path");
    window.history.pushState(null, "", "/reports/");

    expect(basePath()).toBe("");
    expect(publicBaseUrl()).toBe(window.location.origin);
    expect(appPath("/settings/profile/")).toBe("/settings/profile/");
  });

  it("keeps a prefixed entry stable after client-side navigation", async () => {
    vi.resetModules();
    window.history.replaceState(null, "", "/bowhead/");
    document.head.insertAdjacentHTML(
      "beforeend",
      '<meta name="bowhead-internal-path" content="/">',
    );

    const { appPath, basePath, publicBaseUrl } = await import("./base-path");
    window.history.pushState(null, "", "/bowhead/reports/");

    expect(basePath()).toBe("/bowhead");
    expect(publicBaseUrl()).toBe(`${window.location.origin}/bowhead`);
    expect(appPath("/settings/profile/")).toBe(
      "/bowhead/settings/profile/",
    );
  });

  it("reuses the bootstrap entry when the module reloads after navigation", async () => {
    vi.resetModules();
    window.history.replaceState(null, "", "/bowhead/reports/");
    window.__BOWHEAD_BASE_PATH__ = "/bowhead";
    document.head.insertAdjacentHTML(
      "beforeend",
      '<meta name="bowhead-internal-path" content="/">',
    );

    const { appPath, basePath } = await import("./base-path");

    expect(basePath()).toBe("/bowhead");
    expect(appPath("/settings/profile/")).toBe(
      "/bowhead/settings/profile/",
    );
  });
});
