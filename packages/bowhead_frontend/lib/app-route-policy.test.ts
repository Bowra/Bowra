import { afterEach, describe, expect, it } from "vitest";

import { appRoutePolicy } from "./app-route-policy";

afterEach(() => {
  window.__BOWHEAD_PUBLIC_CONTEXT__ = undefined;
  window.__BOWHEAD_BASE_PATH__ = undefined;
});

describe("appRoutePolicy", () => {
  it.each(["/", "/reports/", "/help/", "/settings/profile/"])(
    "uses the protected product shell for %s",
    (pathname) => {
      expect(appRoutePolicy(pathname)).toEqual({
        auth: "required",
        chrome: "product-shell",
      });
    },
  );

  it.each(["/empty-workspace/", "/onboarding/profile/"])(
    "uses the protected standalone surface for %s",
    (pathname) => {
      expect(appRoutePolicy(pathname)).toEqual({
        auth: "required",
        chrome: "standalone",
      });
    },
  );

  it.each(["/spike/p31/", "/spike/chart-audit/"])(
    "keeps deterministic development route %s outside the auth gate",
    (pathname) => {
      expect(appRoutePolicy(pathname)).toEqual({
        auth: "development-bypass",
        chrome: "standalone",
      });
    },
  );

  it("classifies a route below the runtime /bowhead prefix", () => {
    window.__BOWHEAD_PUBLIC_CONTEXT__ = {
      entryId: "entry_1",
      teamId: "team_1",
      baseUrl: "https://example.test/bowhead",
      basePath: "/bowhead",
      matchSource: "trusted_proxy",
    };

    expect(appRoutePolicy("/bowhead/onboarding/profile/")).toEqual({
      auth: "required",
      chrome: "standalone",
    });
    expect(appRoutePolicy("/bowhead/reports/")).toEqual({
      auth: "required",
      chrome: "product-shell",
    });
  });
});
