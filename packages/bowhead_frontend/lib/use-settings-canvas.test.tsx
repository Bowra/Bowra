import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { readSettingsCanvasTarget, settingsCanvasHref, settingsCanvasKey } from "./settings-canvas-target";
import { useSettingsCanvas } from "./use-settings-canvas";

const target = {
  page: "workspace_i18n" as const,
  userId: "u_owner",
  teamId: "team_original",
  workspaceId: "ws_original",
};
const options = () => ({
  enabled: true,
  scope: target,
  canManageSpace: true,
  searchParams: new URLSearchParams(window.location.search),
  sessionId: "s_original" as string | null,
});
afterEach(() => {
  cleanup();
  window.history.replaceState(null, "", "/");
});

it("keeps the admitted page while switching views and cannot reopen from a dismissed URL echo", () => {
  window.history.replaceState(null, "", settingsCanvasHref(target));
  const { result, rerender } = renderHook(useSettingsCanvas, {
    initialProps: options(),
  });
  expect(result.current.target).toEqual(target);
  act(() => result.current.deselect());
  expect(result.current.selected).toBe(false);
  expect(result.current.target).toEqual(target);
  act(() => result.current.close());
  rerender(options());
  expect(result.current.target).toBeNull();
  window.history.replaceState(null, "", settingsCanvasHref(target));
  rerender(options());
  expect(result.current.target).toEqual(target);
});

it("requires both capability and current Space-admin admission", () => {
  window.history.replaceState(null, "", settingsCanvasHref(target));
  const { result, rerender } = renderHook(useSettingsCanvas, {
    initialProps: { ...options(), canManageSpace: false },
  });
  expect(result.current.target).toBeNull();
  rerender({ ...options(), enabled: false });
  expect(result.current.target).toBeNull();
  rerender(options());
  expect(result.current.target).toEqual(target);
  rerender({ ...options(), scope: { ...target, workspaceId: "ws_other" } });
  expect(result.current.target).toBeNull();
  expect(window.location.search).toBe("");
});

it("rejects a foreign caller and detaches an open page on conversation change", () => {
  window.history.replaceState(null, "", settingsCanvasHref({ ...target, userId: "u_other" }));
  const { result, rerender } = renderHook(useSettingsCanvas, {
    initialProps: options(),
  });
  expect(result.current.target).toBeNull();
  window.history.replaceState(null, "", settingsCanvasHref(target));
  rerender(options());
  expect(result.current.target).toEqual(target);
  rerender({ ...options(), sessionId: "s_other" });
  expect(result.current.target).toBeNull();
});

it("retains the page when a first send assigns the new conversation", () => {
  window.history.replaceState(null, "", settingsCanvasHref(target));
  const { result, rerender } = renderHook(useSettingsCanvas, {
    initialProps: { ...options(), sessionId: null as string | null },
  });
  rerender({ ...options(), sessionId: "s_first" });
  expect(result.current.target).toEqual(target);
});

it("restores a retained settings page without taking selection from the Agent", () => {
  window.history.replaceState(null, "", `${settingsCanvasHref(target)}&canvas_settings_active=0`);
  const { result } = renderHook(useSettingsCanvas, { initialProps: options() });
  expect(result.current.target).toEqual(target);
  expect(result.current.selected).toBe(false);
  act(() => result.current.select());
  expect(window.location.search).not.toContain("canvas_settings_active=0");
  act(() => result.current.deselect());
  expect(window.location.search).toContain("canvas_settings_active=0");
});

it("routes a Tool pointer without stealing focus or reopening a dismissed replay", () => {
  const { result } = renderHook(useSettingsCanvas, { initialProps: options() });
  act(() => result.current.open(target, "t_open"));
  expect(result.current.target).toEqual(target);
  act(() => result.current.deselect());
  act(() => result.current.open(target, "t_background"));
  expect(result.current.selected).toBe(false);
  act(() => result.current.close());
  act(() => result.current.open(target, "t_background"));
  expect(result.current.target).toBeNull();
  act(() => result.current.open({ ...target, workspaceId: "ws_other" }, "t_foreign"));
  expect(result.current.target).toBeNull();
});

it("retains the selected editor when the host admits a new background Tool object", () => {
  const { result } = renderHook(useSettingsCanvas, { initialProps: options() });
  act(() => result.current.open(target, "t_background_first", false));
  expect(result.current.target).toEqual(target);
  expect(result.current.selected).toBe(false);
  act(() => result.current.select());
  expect(result.current.selected).toBe(true);
});

it("retains original settings controllers when another page opens in the background", () => {
  const { result } = renderHook(useSettingsCanvas, { initialProps: options() });
  const general = { ...target, page: "space_general" as const };
  act(() => result.current.open(target, "t_preferences"));
  act(() => result.current.open(general, "t_general", false));
  expect(result.current.targets).toEqual([target, general]);
  expect(result.current.target).toEqual(target);
  expect(result.current.selected).toBe(true);
  act(() => result.current.close(settingsCanvasKey(general)));
  expect(result.current.target).toEqual(target);
  act(() => result.current.open(general, "t_general", false));
  expect(result.current.targets).toEqual([target]);
});

it("restores the selected settings page after reload without losing the conversation or entry prefix", () => {
  const general = { ...target, page: "space_general" as const };
  window.history.replaceState(null, "", "/bowhead/?session=s_original&report=r_original#results");
  const view = renderHook(useSettingsCanvas, { initialProps: options() });
  act(() => {
    view.result.current.open(target, "t_preferences");
    view.result.current.open(general, "t_general", false);
  });
  expect(view.result.current.targets).toEqual([target, general]);
  act(() => view.result.current.select(settingsCanvasKey(general)));
  expect(readSettingsCanvasTarget(new URLSearchParams(window.location.search))).toEqual(general);
  expect(window.location.pathname).toBe("/bowhead/");
  expect(window.location.hash).toBe("#results");
  expect(new URLSearchParams(window.location.search).get("session")).toBe("s_original");
  expect(new URLSearchParams(window.location.search).get("report")).toBe("r_original");
  view.unmount();
  const restored = renderHook(useSettingsCanvas, { initialProps: options() });
  expect(restored.result.current.target).toEqual(general);
  expect(restored.result.current.selected).toBe(true);
});

it("a retained close callback preserves newer pages and their selected URL", () => {
  const general = { ...target, page: "space_general" as const };
  const { result, rerender } = renderHook(useSettingsCanvas, { initialProps: options() });
  act(() => result.current.open(target, "t_preferences"));
  const originalClose = result.current.close;
  act(() => result.current.open(general, "t_general", false));
  act(() => result.current.select(settingsCanvasKey(general)));
  act(() => originalClose(settingsCanvasKey(target)));
  rerender(options());
  expect(result.current.targets).toEqual([general]);
  expect(result.current.target).toEqual(general);
  expect(result.current.selected).toBe(true);
  expect(readSettingsCanvasTarget(new URLSearchParams(window.location.search))).toEqual(general);
});

it("closing a retained settings page cannot steal selection back from Results", () => {
  const general = { ...target, page: "space_general" as const };
  const { result, rerender } = renderHook(useSettingsCanvas, { initialProps: options() });
  act(() => result.current.open(target, "t_preferences"));
  act(() => result.current.open(general, "t_general", false));
  act(() => result.current.deselect());
  act(() => result.current.close(settingsCanvasKey(target)));
  rerender(options());
  expect(result.current.target).toEqual(general);
  expect(result.current.selected).toBe(false);
  expect(readSettingsCanvasTarget(new URLSearchParams(window.location.search))).toEqual(general);
  expect(window.location.search).toContain("canvas_settings_active=0");
});

it("respects a changed history selection for the same retained page and rejects unknown selections", () => {
  window.history.replaceState(null, "", settingsCanvasHref(target));
  const { result, rerender } = renderHook(useSettingsCanvas, { initialProps: options() });
  window.history.replaceState(null, "", `${settingsCanvasHref(target)}&canvas_settings_active=0`);
  rerender(options());
  expect(result.current.selected).toBe(false);
  act(() => result.current.select(settingsCanvasKey({ ...target, workspaceId: "ws_other" })));
  expect(result.current.selected).toBe(false);
  window.history.replaceState(null, "", settingsCanvasHref(target));
  rerender(options());
  expect(result.current.selected).toBe(true);
});
