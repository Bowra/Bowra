import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({
  bootstrap: vi.fn(),
  beginTransition: vi.fn(),
  activateCached: vi.fn(),
  syncStorage: vi.fn(),
  syncThemeColor: vi.fn(),
  pathname: "/",
}));

vi.mock("next/navigation", () => ({
  usePathname: () => state.pathname,
}));
vi.mock("@/lib/use-theme-config", () => ({
  bootstrapEffectiveTheme: state.bootstrap,
}));
vi.mock("@/lib/effective-theme-request-guard", () => ({
  beginBrandTransition: state.beginTransition,
}));
vi.mock("@/lib/brand-runtime", () => ({
  activateCachedBrandForTeam: state.activateCached,
  syncBrandThemeColor: state.syncThemeColor,
  syncBrandFromStorage: state.syncStorage,
}));

import { ThemeBootstrap } from "./theme-bootstrap";
import { TEAM_CHANGED_EVENT } from "@/lib/team-storage";

afterEach(() => {
  cleanup();
  state.bootstrap.mockReset();
  state.beginTransition.mockReset();
  state.activateCached.mockReset();
  state.syncStorage.mockReset();
  state.syncThemeColor.mockReset();
  state.pathname = "/";
  window.history.replaceState({}, "", "/");
});

describe("ThemeBootstrap", () => {
  it("loads the effective theme on normal application routes", async () => {
    render(<ThemeBootstrap />);

    await waitFor(() => expect(state.bootstrap).toHaveBeenCalledOnce());
  });

  it("refreshes with the team ID carried by the switch event", async () => {
    render(<ThemeBootstrap />);
    await waitFor(() => expect(state.bootstrap).toHaveBeenCalledOnce());

    window.dispatchEvent(
      new CustomEvent(TEAM_CHANGED_EVENT, {
        detail: { teamId: "team_b" },
      }),
    );

    expect(state.beginTransition).toHaveBeenCalledWith("team_b");
    expect(state.activateCached).toHaveBeenCalledWith("team_b");
    expect(state.bootstrap).toHaveBeenLastCalledWith("team_b");
  });

  it("does not make a live API request on the snapshot-only export route", () => {
    window.history.replaceState({}, "", "/internal/canvas-export/visual/");

    render(<ThemeBootstrap />);

    expect(state.bootstrap).not.toHaveBeenCalled();
  });

  it.each([
    "/activate",
    "/activate/",
    "/login",
    "/login/",
    "/register",
    "/forgot-password",
    "/reset-password",
    "/invite/ready",
  ])(
    "does not request the authenticated theme endpoint on public route %s",
    (pathname) => {
      window.history.replaceState({}, "", pathname);

      render(<ThemeBootstrap />);

      expect(state.bootstrap).not.toHaveBeenCalled();
    },
  );

  it("does not request a theme while Next router state still points to another page", () => {
    state.pathname = "/";
    window.history.replaceState({}, "", "/activate/");

    render(<ThemeBootstrap />);

    expect(state.bootstrap).not.toHaveBeenCalled();
  });
});
