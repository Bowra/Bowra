import { describe, expect, it } from "vitest";

import type { MeUser } from "@/lib/auth-api";
import { composerCapabilitiesForUser } from "./use-composer-capabilities";

function userWithCapabilities(
  capabilities: NonNullable<MeUser["capabilities"]>,
): MeUser {
  return { capabilities } as MeUser;
}

describe("composerCapabilitiesForUser", () => {
  it("fails closed without an authenticated capability projection", () => {
    expect(composerCapabilitiesForUser(null)).toEqual({
      planning: false,
    });
  });

  it("consumes the Gateway planning capability even without composer rollout fields", () => {
    expect(
      composerCapabilitiesForUser(userWithCapabilities({ planning: true })),
    ).toMatchObject({ planning: true });
  });

  it("does not project stale Composer rollout fields into product capabilities", () => {
    const capabilities = composerCapabilitiesForUser(
      userWithCapabilities({
        composer: {
          model_panel_v2: true,
          thinking_effort: true,
        },
        planning: false,
      }),
    );

    expect(capabilities).toEqual({
      planning: false,
    });
  });
});
