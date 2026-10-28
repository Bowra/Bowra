import { describe, expect, it } from "vitest";

import enUS from "../messages/en-US.json";
import zhCN from "../messages/zh-CN.json";
import { ApiError } from "./api-client";
import { kbRecordError } from "./kb-record-error";

type Bundle = {
  records: {
    errors: Record<string, string>;
  };
};

describe("kbRecordError", () => {
  it("maps finite codes to bilingual messages", () => {
    const cases = [
      ["kb_record_not_found", "errors.not_found"],
      ["invalid_kb_record_value", "errors.invalid_value"],
      ["kb_record_service_unavailable", "errors.service_unavailable"],
      ["workspace_member_role_required", "errors.access_denied"],
    ] as const;
    for (const [code, key] of cases) {
      expect(
        kbRecordError(new ApiError(400, "RAW_STORE_PROSE", { code }), "edit"),
      ).toBe(key);
      const leaf = key.replace("errors.", "");
      expect((zhCN as Bundle).records.errors[leaf]).toBeTruthy();
      expect((enUS as Bundle).records.errors[leaf]).toBeTruthy();
    }
  });

  it("ignores unknown backend prose and uses safe status/operation fallbacks", () => {
    expect(
      kbRecordError(
        new ApiError(500, "SQLITE_PATH_AND_VALUE", {
          code: "future_record_code",
        }),
        "status",
      ),
    ).toBe("errors.service_unavailable");
    expect(kbRecordError(new Error("network token"), "load")).toBe(
      "errors.load_failed",
    );
  });
});
