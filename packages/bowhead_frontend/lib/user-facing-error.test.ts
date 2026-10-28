import { afterEach, describe, expect, it } from "vitest";

import {
  genericUserFacingErrorMessage,
  safeUserFacingErrorMessage,
} from "./user-facing-error";

afterEach(() => {
  document.documentElement.lang = "";
});

describe("user-facing error fallback", () => {
  it("uses the Chinese generic message for the Chinese UI", () => {
    document.documentElement.lang = "zh-CN";
    expect(genericUserFacingErrorMessage()).toBe(
      "操作失败，请稍后重试；如问题持续，请联系管理员。",
    );
  });

  it("uses the English generic message for the English UI", () => {
    document.documentElement.lang = "en-US";
    expect(genericUserFacingErrorMessage()).toBe(
      "Something went wrong. Please try again. If the problem continues, contact your administrator.",
    );
  });

  it("does not expose a backend code as the visible message", () => {
    document.documentElement.lang = "en-US";
    expect(
      safeUserFacingErrorMessage("team_not_found", "team_not_found"),
    ).not.toContain("team_not_found");
  });

  it("uses the French generic message when an internal code needs a fallback", () => {
    document.documentElement.lang = "fr-FR";
    expect(safeUserFacingErrorMessage("team_not_found", "team_not_found")).toBe(
      "Une erreur s’est produite. Réessayez. Si le problème persiste, contactez votre administrateur.",
    );
  });

  it("preserves a real backend message", () => {
    expect(
      safeUserFacingErrorMessage(
        "This invitation has expired.",
        "invite_expired",
      ),
    ).toBe("This invitation has expired.");
  });
});
