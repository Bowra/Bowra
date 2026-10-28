import { afterEach, describe, expect, it } from "vitest";

import {
  acceptEffectiveThemeResponse,
  beginBrandTransition,
  beginEffectiveThemeRequest,
  getVerifiedEffectiveThemeTeamId,
  resetEffectiveThemeRequestGuardForTests,
} from "./effective-theme-request-guard";
import {
  canonicalEffectiveThemeFallback,
  type EffectiveThemeResponse,
} from "./theme-config";

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

afterEach(() => {
  resetEffectiveThemeRequestGuardForTests();
});

describe("effective theme request guard", () => {
  it("accepts the first server-verified team and pins later requests to it", () => {
    const initial = beginEffectiveThemeRequest();
    expect(acceptEffectiveThemeResponse(initial.token, response("team_a"))).toBe(
      true,
    );
    initial.complete();
    expect(getVerifiedEffectiveThemeTeamId()).toBe("team_a");

    const next = beginEffectiveThemeRequest();
    expect(next.token.expectedTeamId).toBe("team_a");
    expect(acceptEffectiveThemeResponse(next.token, response("team_b"))).toBe(
      false,
    );
    next.complete();
  });

  it("accepts only C when A to B to C responses return out of order", () => {
    const requestA = beginEffectiveThemeRequest();
    beginBrandTransition("team_b");
    expect(requestA.signal.aborted).toBe(true);
    const requestB = beginEffectiveThemeRequest("team_b");
    beginBrandTransition("team_c");
    expect(requestB.signal.aborted).toBe(true);
    const requestC = beginEffectiveThemeRequest("team_c");

    expect(
      acceptEffectiveThemeResponse(requestC.token, response("team_c")),
    ).toBe(true);
    expect(
      acceptEffectiveThemeResponse(requestA.token, response("team_a")),
    ).toBe(false);
    expect(
      acceptEffectiveThemeResponse(requestB.token, response("team_b")),
    ).toBe(false);
    expect(getVerifiedEffectiveThemeTeamId()).toBe("team_c");
    requestA.complete();
    requestB.complete();
    requestC.complete();
  });

  it("rejects a current-epoch response for the wrong expected team", () => {
    beginBrandTransition("team_b");
    const request = beginEffectiveThemeRequest();

    expect(request.token.expectedTeamId).toBe("team_b");
    expect(
      acceptEffectiveThemeResponse(request.token, response("team_a")),
    ).toBe(false);
    expect(getVerifiedEffectiveThemeTeamId()).toBeNull();
    request.complete();
  });
});
