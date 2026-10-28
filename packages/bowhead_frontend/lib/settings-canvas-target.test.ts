import { afterEach, expect, it } from "vitest";
import {
  clearSettingsCanvasTargetInUrl,
  readSettingsCanvasTarget,
  settingsCanvasHref,
  settingsCanvasKey,
  settingsCanvasTargetMatches,
} from "./settings-canvas-target";

const target = {
  page: "workspace_i18n" as const,
  userId: "u_owner",
  teamId: "team_original",
  workspaceId: "ws_original",
};
afterEach(() => window.history.replaceState(null, "", "/"));

it("restores an exact settings target alongside the existing conversation and Agent draft", () => {
  const href = settingsCanvasHref(
    target,
    "/?session=s_original&agent_draft=sad_original",
  );
  const params = new URL(href, window.location.origin).searchParams;
  expect(readSettingsCanvasTarget(params)).toEqual(target);
  expect(params.get("session")).toBe("s_original");
  expect(params.get("agent_draft")).toBe("sad_original");
  window.history.replaceState(null, "", `${href}#section`);
  clearSettingsCanvasTargetInUrl();
  expect(window.location.search).toBe(
    "?session=s_original&agent_draft=sad_original",
  );
  expect(window.location.hash).toBe("#section");
});

it("rejects unsupported pages and incomplete identities without inferring a Space", () => {
  const params = new URL(settingsCanvasHref(target), window.location.origin)
    .searchParams;
  params.delete("canvas_space");
  expect(readSettingsCanvasTarget(params)).toBeNull();
  params.set("canvas_space", target.workspaceId);
  params.set("canvas_settings", "system_secrets");
  expect(readSettingsCanvasTarget(params)).toBeNull();
  expect(
    settingsCanvasHref(target, "/agents/manage/?agent_id=osa_other"),
  ).not.toContain("agent_id");
});

it("isolates identical page names by caller, Team and Space", () => {
  for (const field of ["userId", "teamId", "workspaceId"] as const) {
    const other = { ...target, [field]: "other" };
    expect(settingsCanvasTargetMatches(target, other)).toBe(false);
    expect(settingsCanvasKey(other)).not.toBe(settingsCanvasKey(target));
  }
});
