import { describe, expect, it } from "vitest";

import { ApiError } from "./api-client";
import { ruleClarificationErrorPresentation } from "./rule-clarification-error";

const copy = {
  generic: "The action could not be completed.",
  entitlementDenied: "Contact an administrator about your authorization.",
  transport: {
    requestTimedOut: "The request timed out.",
    serviceUnreachable: "The service could not be reached.",
    retry: "Try again",
  },
};

describe("ruleClarificationErrorPresentation", () => {
  it("shows catalog copy only for the matching named publisher", () => {
    const error = new ApiError(
      422,
      "The submitted content does not meet the safety requirements.",
      {
        code: "clarification_input_rejected",
        diagnostics: {
          schemaVersion: 1,
          disclosure: "authenticated",
          status: 422,
          code: "clarification_input_rejected",
          diagnosticCode: "rule_clarification_failure",
          safeDetail: "phase=validate_input; failure_class=validation",
          retryable: false,
        },
      },
    );

    const presentation = ruleClarificationErrorPresentation(error, {
      surface: "chat",
      copy,
    });

    expect(presentation.message).toBe(error.message);
    expect(presentation.diagnostics?.code).toBe(
      "clarification_input_rejected",
    );
  });

  it("hides unregistered backend prose but keeps safe transport facts", () => {
    const error = new ApiError(
      500,
      "Traceback: password=raw-secret at /srv/private/app.py",
      {
        code: "private_store_exception",
        requestId: "0123456789abcdef0123456789abcdef",
      },
    );

    const presentation = ruleClarificationErrorPresentation(error, {
      surface: "settings",
      copy,
    });

    expect(presentation.message).toBe(copy.generic);
    expect(JSON.stringify(presentation)).not.toContain("raw-secret");
    expect(presentation.diagnostics).toMatchObject({
      status: 500,
      requestId: "0123456789abcdef0123456789abcdef",
    });
    expect(presentation.diagnostics?.code).toBeUndefined();
  });

  it("keeps an unclassified exception distinct from a transport failure", () => {
    const presentation = ruleClarificationErrorPresentation(
      new Error("browser public URL is not registered: raw-secret"),
      {
        surface: "chat",
        copy,
      },
    );

    expect(presentation.message).toBe(copy.generic);
    expect(JSON.stringify(presentation)).not.toContain("raw-secret");
    expect(presentation.diagnostics).toBeUndefined();
    expect(presentation.action).toBeUndefined();
  });

  it("uses the local entitlement message for code-only gate failures", () => {
    const presentation = ruleClarificationErrorPresentation(
      new ApiError(403, "needs_license", {
        code: "needs_license",
      }),
      {
        surface: "chat",
        copy,
      },
    );

    expect(presentation.message).toBe(copy.entitlementDenied);
    expect(presentation.diagnostics?.code).toBe("needs_license");
  });

  it.each([
    ["user_credit_exhausted", "你的本期额度已用完"],
    ["team_pool_exhausted", "团队本期总额度已用完"],
  ])("tells the user credits ran out instead of blaming authorization (%s)", (code, expected) => {
    const presentation = ruleClarificationErrorPresentation(
      new ApiError(402, code, { code }),
      {
        surface: "chat",
        copy,
      },
    );

    expect(presentation.message).toContain(expected);
    expect(presentation.message).not.toBe(copy.entitlementDenied);
    expect(presentation.diagnostics?.code).toBe(code);
  });
});
