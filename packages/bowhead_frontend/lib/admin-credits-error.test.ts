import { describe, expect, it } from "vitest";

import messagesEn from "@/messages/en-US.json";
import messagesZh from "@/messages/zh-CN.json";

import { AdminCreditsError } from "./admin-credits-api";
import {
  ADMIN_CREDITS_ERROR_KEY,
  adminCreditsErrorKey,
} from "./admin-credits-error";

describe("admin credits error contract", () => {
  it("has a non-empty zh-CN and en-US message for every mapped code", () => {
    const zh = messagesZh.admin.credits as Record<string, unknown>;
    const en = messagesEn.admin.credits as Record<string, unknown>;

    for (const key of Object.values(ADMIN_CREDITS_ERROR_KEY)) {
      expect(zh[key], `missing zh-CN admin.credits.${key}`).toEqual(
        expect.any(String),
      );
      expect(en[key], `missing en-US admin.credits.${key}`).toEqual(
        expect.any(String),
      );
      expect((zh[key] as string).trim()).not.toBe("");
      expect((en[key] as string).trim()).not.toBe("");
    }
  });

  it("maps business codes without reading backend prose", () => {
    expect(
      adminCreditsErrorKey(
        new AdminCreditsError(
          409,
          "RAW_BACKEND_POOL_DIAGNOSTIC",
          undefined,
          "credit_team_pool_insufficient",
        ),
      ),
    ).toBe("err_team_pool_insufficient");
    expect(
      adminCreditsErrorKey(
        new AdminCreditsError(
          403,
          "RAW_BACKEND_CAPABILITY_DIAGNOSTIC",
          undefined,
          "insufficient_credit_capability",
        ),
      ),
    ).toBe("err_insufficient_capability");
  });

  it("uses safe validation, unknown-API, and network fallbacks", () => {
    expect(
      adminCreditsErrorKey(
        new AdminCreditsError(422, "Input should be less than 500"),
      ),
    ).toBe("err_invalid_fields");
    expect(
      adminCreditsErrorKey(
        new AdminCreditsError(
          500,
          "traceback detail",
          undefined,
          "future_internal_code",
        ),
      ),
    ).toBe("err_generic");
    expect(adminCreditsErrorKey(new TypeError("Failed to fetch"))).toBe(
      "err_network",
    );
  });
});
