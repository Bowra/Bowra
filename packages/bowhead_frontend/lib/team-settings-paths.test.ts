// @vitest-environment node
import { describe, expect, it } from "vitest";

import { modelSettingsOverviewPath, teamSettingsPath } from "./team-settings-paths";

describe("teamSettingsPath", () => {
  it("uses the user-facing models segment for the internal llm tab id", () => {
    expect(teamSettingsPath("llm")).toBe("/team/settings/models/");
    expect(teamSettingsPath("llm", true)).toBe(
      "/team/settings/models/?from=setup",
    );
  });

  it("addresses the model overview presentation tab with a fragment", () => {
    expect(modelSettingsOverviewPath("assignments")).toBe("/team/settings/models/#assignments");
    expect(modelSettingsOverviewPath()).toBe("/team/settings/models/#connections");
  });

  it("uses canonical segments that match their team settings pages", () => {
    expect(teamSettingsPath("people")).toBe("/team/settings/people/");
    expect(teamSettingsPath("workspaces")).toBe("/team/settings/spaces/");
    expect(teamSettingsPath("public-entries")).toBe(
      "/team/settings/public-entries/",
    );
    expect(teamSettingsPath("datasource")).toBe("/team/settings/datasource/");
    expect(teamSettingsPath("team-appearance")).toBe(
      "/team/settings/brand-appearance/",
    );
  });
});
