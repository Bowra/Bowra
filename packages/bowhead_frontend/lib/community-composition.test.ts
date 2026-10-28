import { describe, expect, it } from "vitest";
import { BUILD_EDITION, PRODUCT_NAME } from "./product-identity";
import { canonicalEffectiveThemeFallback } from "./theme-config";
import type { MeUser } from "./auth-api";
import { centerNavigationGroups } from "./navigation-centers";
import { visibleSettingsNavigation } from "./settings-destinations";
import { ADDITIONAL_OPENAPI_SCOPES } from "@/edition/openapi-scopes";

describe.runIf(BUILD_EDITION === "community")("Community composition", () => {
  it("uses the Bowra identity with the canonical shared rendering contract", () => {
    expect(PRODUCT_NAME).toBe("Bowra");
    expect(canonicalEffectiveThemeFallback().brand?.applicationName).toBe("Bowra");
  });
  it("offers owners the shared administration and chart-style destinations", () => {
    const groups = visibleSettingsNavigation(
      {
        teamRole: "owner",
        capabilities: {},
        isLocalDeployment: true,
        teamOpenapiServed: true,
        canManageActiveSpace: true,
      },
      { tab: null, admin: null },
    );
    const consoleGroups = centerNavigationGroups("administration", {
      team_role: "owner",
      capabilities: { credit: { credits_read: true } },
    } as MeUser);
    expect(consoleGroups.flatMap((group) => group.items.map((item) => item.key))).toEqual([
      "admin:traces", "admin:approvals", "admin:credits", "admin:access",
    ]);
    expect(groups.flatMap((group) => group.items).map((item) => item.key)).toEqual(
      expect.arrayContaining(["tab:appearance", "tab:team-appearance", "tab:openapi"]),
    );
    expect(ADDITIONAL_OPENAPI_SCOPES).toEqual([]);
  });
});
