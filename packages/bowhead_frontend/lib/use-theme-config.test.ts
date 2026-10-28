import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  beginBrandTransition,
  resetEffectiveThemeRequestGuardForTests,
} from "./effective-theme-request-guard";
import {
  THEME_CONFIG_CHANGED_EVT,
  canonicalEffectiveThemeFallback,
  type EffectiveThemeResponse,
} from "./theme-config";

const state = vi.hoisted(() => ({
  getEffectiveTheme: vi.fn(),
  applyBrandBundle: vi.fn(),
}));

vi.mock("./api", () => ({
  deleteUserSetting: vi.fn(),
  getEffectiveTheme: state.getEffectiveTheme,
  setUserSetting: vi.fn(),
}));
vi.mock("./brand-runtime", () => ({
  applyBrandBundleToDocument: state.applyBrandBundle,
}));

import {
  refreshEffectiveTheme,
  useThemeConfig,
} from "./use-theme-config";

function response(teamId: string): EffectiveThemeResponse {
  const bundle = canonicalEffectiveThemeFallback();
  bundle.schemaVersion = 2;
  bundle.team = {
    id: teamId,
    themeRevision: 1,
    revisionId: `tbr_${"1".repeat(24)}`,
    configurationType: "bowhead",
  };
  return bundle;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

afterEach(() => {
  state.getEffectiveTheme.mockReset();
  state.applyBrandBundle.mockReset();
  resetEffectiveThemeRequestGuardForTests();
});

describe("guarded effective theme refresh", () => {
  it("applies only C when A, B and C complete out of order", async () => {
    const requestA = deferred<EffectiveThemeResponse>();
    const requestB = deferred<EffectiveThemeResponse>();
    const requestC = deferred<EffectiveThemeResponse>();
    state.getEffectiveTheme
      .mockReturnValueOnce(requestA.promise)
      .mockReturnValueOnce(requestB.promise)
      .mockReturnValueOnce(requestC.promise);

    const refreshA = refreshEffectiveTheme();
    beginBrandTransition("team_b");
    const refreshB = refreshEffectiveTheme("team_b");
    beginBrandTransition("team_c");
    const refreshC = refreshEffectiveTheme("team_c");

    requestC.resolve(response("team_c"));
    expect(await refreshC).toBe(true);
    requestA.resolve(response("team_a"));
    expect(await refreshA).toBe(false);
    requestB.resolve(response("team_b"));
    expect(await refreshB).toBe(false);

    expect(state.applyBrandBundle).toHaveBeenCalledOnce();
    expect(
      (state.applyBrandBundle.mock.calls[0]?.[0] as EffectiveThemeResponse)
        .team?.id,
    ).toBe("team_c");
  });

  it("drops a current response whose team does not match the transition", async () => {
    beginBrandTransition("team_b");
    state.getEffectiveTheme.mockResolvedValue(response("team_a"));

    await expect(refreshEffectiveTheme("team_b")).resolves.toBe(false);

    expect(state.applyBrandBundle).not.toHaveBeenCalled();
  });

  it("does not apply a pending refresh after the hook unmounts", async () => {
    const pending = deferred<EffectiveThemeResponse>();
    state.getEffectiveTheme.mockReturnValue(pending.promise);
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined);

    const view = renderHook(() => useThemeConfig());
    await vi.waitFor(() => expect(state.getEffectiveTheme).toHaveBeenCalled());
    view.unmount();
    await act(async () => {
      pending.resolve(response("team_a"));
      await pending.promise;
    });

    expect(state.applyBrandBundle).not.toHaveBeenCalled();
    expect(
      consoleError.mock.calls.some(([message]) =>
        String(message).includes("unmounted component"),
      ),
    ).toBe(false);
    consoleError.mockRestore();
  });

  it("does not refetch when applying the server response emits an internal theme event", async () => {
    state.getEffectiveTheme.mockResolvedValue(response("team_a"));
    state.applyBrandBundle.mockImplementationOnce(() => {
      window.dispatchEvent(new CustomEvent(THEME_CONFIG_CHANGED_EVT));
    });

    const view = renderHook(() => useThemeConfig());

    await vi.waitFor(() => expect(view.result.current.isLoading).toBe(false));
    await Promise.resolve();
    expect(state.getEffectiveTheme).toHaveBeenCalledOnce();
    view.unmount();
  });

  it("still refreshes when another writer emits a theme event", async () => {
    state.getEffectiveTheme.mockResolvedValue(response("team_a"));
    const view = renderHook(() => useThemeConfig());
    await vi.waitFor(() => expect(view.result.current.isLoading).toBe(false));

    act(() => {
      window.dispatchEvent(new CustomEvent(THEME_CONFIG_CHANGED_EVT));
    });

    await vi.waitFor(() => expect(state.getEffectiveTheme).toHaveBeenCalledTimes(2));
    view.unmount();
  });

  it("queues only one hook refresh when the global refresh applies a theme", async () => {
    state.getEffectiveTheme.mockResolvedValue(response("team_a"));
    state.applyBrandBundle.mockImplementation(() => {
      window.dispatchEvent(new CustomEvent(THEME_CONFIG_CHANGED_EVT));
    });
    const view = renderHook(() => useThemeConfig());
    await vi.waitFor(() => expect(view.result.current.isLoading).toBe(false));
    expect(state.getEffectiveTheme).toHaveBeenCalledOnce();

    await act(async () => {
      await refreshEffectiveTheme();
    });

    await vi.waitFor(() => expect(state.getEffectiveTheme).toHaveBeenCalledTimes(3));
    await Promise.resolve();
    expect(state.getEffectiveTheme).toHaveBeenCalledTimes(3);
    view.unmount();
  });
});
