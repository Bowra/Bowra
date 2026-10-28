import { describe, expect, it } from "vitest";

import messagesEn from "@/messages/en-US.json";
import messagesZh from "@/messages/zh-CN.json";

import { ApiError } from "./api-client";
import {
  SPACE_MEMBER_ERROR_KEY,
  spaceMemberErrorKey,
} from "./space-member-error";

describe("space member error contract", () => {
  it("has a non-empty zh-CN and en-US message for every mapped code", () => {
    const zh = messagesZh.settings.members as Record<string, unknown>;
    const en = messagesEn.settings.members as Record<string, unknown>;

    for (const key of Object.values(SPACE_MEMBER_ERROR_KEY)) {
      expect(zh[key], `missing zh-CN settings.members.${key}`).toEqual(
        expect.any(String),
      );
      expect(en[key], `missing en-US settings.members.${key}`).toEqual(
        expect.any(String),
      );
      expect((zh[key] as string).trim()).not.toBe("");
      expect((en[key] as string).trim()).not.toBe("");
    }
  });

  it("maps known codes without reading backend prose", () => {
    expect(
      spaceMemberErrorKey(
        new ApiError(409, "raw internal detail", {
          code: "workspace_team_mismatch",
        }),
      ),
    ).toBe("err_workspace_team_mismatch");
  });

  it("localizes validation, unknown API, and network failures safely", () => {
    expect(
      spaceMemberErrorKey(
        new ApiError(422, "Input should match pattern"),
        "err_invalid_role",
      ),
    ).toBe("err_invalid_role");
    expect(
      spaceMemberErrorKey(
        new ApiError(500, "traceback", { code: "future_internal_code" }),
      ),
    ).toBe("err_generic");
    expect(spaceMemberErrorKey(new TypeError("Failed to fetch"))).toBe(
      "network_error",
    );
  });
});
