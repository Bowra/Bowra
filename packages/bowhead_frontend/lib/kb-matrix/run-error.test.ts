import { describe, expect, it } from "vitest";

import messagesEn from "@/messages/en-US.json";
import messagesZh from "@/messages/zh-CN.json";

import { MatrixApiError } from "./api-error";
import {
  KB_MATRIX_RUN_ERROR_KEY,
  kbMatrixRunErrorKey,
} from "./run-error";

describe("kb matrix run error contract", () => {
  it("has a non-empty zh-CN and en-US message for every mapped code", () => {
    const zh = messagesZh.kbMatrix as Record<string, unknown>;
    const en = messagesEn.kbMatrix as Record<string, unknown>;

    for (const key of Object.values(KB_MATRIX_RUN_ERROR_KEY)) {
      expect(zh[key], `missing zh-CN kbMatrix.${key}`).toEqual(
        expect.any(String),
      );
      expect(en[key], `missing en-US kbMatrix.${key}`).toEqual(
        expect.any(String),
      );
      expect((zh[key] as string).trim()).not.toBe("");
      expect((en[key] as string).trim()).not.toBe("");
    }
  });

  it("maps stable codes without reading backend prose", () => {
    expect(
      kbMatrixRunErrorKey(
        new MatrixApiError(
          503,
          "matrix_service_unavailable",
          "RAW_BACKEND_ENGINE_DIAGNOSTIC",
          {},
        ),
      ),
    ).toBe("err_run_service_unavailable");
    expect(
      kbMatrixRunErrorKey(
        new MatrixApiError(
          403,
          "owner_or_admin_required",
          "RAW_BACKEND_OWNER_DIAGNOSTIC",
          {},
        ),
      ),
    ).toBe("err_run_cancel_forbidden");
    expect(
      kbMatrixRunErrorKey(
        new MatrixApiError(
          403,
          "ws_access_denied:cross_team",
          "RAW_BACKEND_AUTHZ_DIAGNOSTIC",
          {},
        ),
      ),
    ).toBe("err_run_access_denied");
  });

  it("uses safe status and network fallbacks", () => {
    expect(
      kbMatrixRunErrorKey(
        new MatrixApiError(422, "future_validation_code", "field internals", {}),
      ),
    ).toBe("err_run_invalid_request");
    expect(
      kbMatrixRunErrorKey(
        new MatrixApiError(500, "future_internal_code", "traceback", {}),
      ),
    ).toBe("err_run_service_unavailable");
    expect(kbMatrixRunErrorKey(new TypeError("Failed to fetch"))).toBe(
      "err_run_network",
    );
  });
});
