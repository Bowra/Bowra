import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { push, replace } = vi.hoisted(() => ({
  push: vi.fn(),
  replace: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace, back: vi.fn() }),
}));

afterEach(() => {
  vi.resetModules();
  window.__BOWHEAD_PUBLIC_CONTEXT__ = undefined;
  window.__BOWHEAD_BASE_PATH__ = "";
  window.__BOWHEAD_BASE_URL__ = undefined;
  push.mockReset();
  replace.mockReset();
});

describe("app navigation href", () => {
  it("allows an active draft guard to cancel client navigation", async () => {
    const { appNavigationAllowed, BEFORE_APP_NAVIGATION_EVENT } = await import("./use-app-router");
    const guard = (event: Event) => event.preventDefault();
    window.addEventListener(BEFORE_APP_NAVIGATION_EVENT, guard);

    expect(appNavigationAllowed("/team/settings/people/", vi.fn())).toBe(false);

    window.removeEventListener(BEFORE_APP_NAVIGATION_EVENT, guard);
    expect(appNavigationAllowed("/team/settings/people/", vi.fn())).toBe(true);
  });

  it("lets a guard resume the same router navigation after confirming", async () => {
    const { useAppRouter, BEFORE_APP_NAVIGATION_EVENT } = await import("./use-app-router");
    let resume: (() => void) | undefined;
    const guard = (event: Event) => {
      event.preventDefault();
      resume = (event as CustomEvent<{ proceed: () => void }>).detail.proceed;
    };
    window.addEventListener(BEFORE_APP_NAVIGATION_EVENT, guard);
    const { result } = renderHook(() => useAppRouter());

    act(() => result.current.push("/team/settings/people"));
    expect(push).not.toHaveBeenCalled();

    window.removeEventListener(BEFORE_APP_NAVIGATION_EVENT, guard);
    act(() => resume?.());
    expect(push).toHaveBeenCalledWith("/team/settings/people/");
  });

  it("keeps root deployment paths internal to Next Router", async () => {
    window.__BOWHEAD_BASE_PATH__ = "";
    const { appNavigationHref } = await import("./use-app-router");
    expect(appNavigationHref("/")).toBe("/");
    expect(appNavigationHref("/settings/profile")).toBe("/settings/profile/");
  });

  it.each([
    ["/bowhead", "https://deployment.example/bowhead"],
    ["/bowhead/", "https://deployment.example/bowhead/"],
    ["/bowhead", "https://deployment.example/"],
    ["/bowhead/", "https://deployment.example"],
  ])("keeps Next Router targets internal for BASE_PATH %s and BASE_URL %s", async (basePath, publicBaseUrl) => {
    window.__BOWHEAD_BASE_PATH__ = basePath;
    window.__BOWHEAD_BASE_URL__ = publicBaseUrl;
    const { appNavigationHref } = await import("./use-app-router");
    expect(appNavigationHref("/")).toBe("/");
    expect(appNavigationHref("/login/")).toBe("/login/");
    expect(appNavigationHref("/settings/profile")).toBe("/settings/profile/");
    expect(appNavigationHref("/team/settings/tools")).toBe("/team/settings/tools/");
    expect(appNavigationHref("/bowhead/files/")).toBe("/files/");
  });

  it("builds canonical full-document targets from the runtime public-entry prefix", async () => {
    window.__BOWHEAD_BASE_PATH__ = "/ai/apps/bowhead";
    window.__BOWHEAD_BASE_URL__ = "https://deployment.example/ai/apps/bowhead";
    const { appDocumentHref } = await import("./use-app-router");

    expect(appDocumentHref("/")).toBe("/ai/apps/bowhead/");
    expect(appDocumentHref("/settings/profile")).toBe(
      "/ai/apps/bowhead/settings/profile/",
    );
    expect(appDocumentHref("/ai/apps/bowhead/reports/")).toBe(
      "/ai/apps/bowhead/reports/",
    );
  });

  it.each([
    ["", "/", "/login/"],
    ["/bowhead", "/bowhead/", "/bowhead/login/"],
  ])("keeps password recovery destinations under the %s entry", async (prefix, home, login) => {
    window.__BOWHEAD_BASE_PATH__ = prefix;
    const { appDocumentHref } = await import("./use-app-router");

    expect(appDocumentHref("/")).toBe(home);
    expect(appDocumentHref("/login/")).toBe(login);
  });

  it("uses the Next client router under a runtime prefix", async () => {
    window.__BOWHEAD_BASE_PATH__ = "/bowhead";
    window.__BOWHEAD_BASE_URL__ = "https://deployment.example/bowhead";
    const { useAppRouter } = await import("./use-app-router");
    const { result } = renderHook(() => useAppRouter());

    act(() => result.current.push("/reports"));
    act(() => result.current.replace("/bowhead/settings/profile"));

    expect(push).toHaveBeenCalledWith("/reports/");
    expect(replace).toHaveBeenCalledWith("/settings/profile/");
  });
});
