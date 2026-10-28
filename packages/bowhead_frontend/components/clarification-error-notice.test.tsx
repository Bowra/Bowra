import "@testing-library/jest-dom/vitest";
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";
import { ruleClarificationErrorPresentation } from "@/lib/rule-clarification-error";
import en from "../../../config/i18n/errors/en-US.json";
import zh from "../../../config/i18n/errors/zh-CN.json";
import { ErrorNotice } from "./error-notice";

describe("pending clarification notice", () => {
  it.each([
    ["en-US", en, "Keep your answer"],
    ["zh-CN", zh, "请保留当前回答"],
  ] as const)("keeps the answer pending without claiming saved or lost context (%s)", (locale, catalog, expected) => {
    const code = "clarification_pause_pending";
    const error = new ApiError(409, catalog[code], {
      code, diagnostics: {
        schemaVersion: 1, disclosure: "authenticated", status: 409, code,
        diagnosticCode: "rule_clarification_failure", safeDetail: "phase=state_change; failure_class=state", retryable: true,
      },
    });
    const presentation = ruleClarificationErrorPresentation(error, {
      surface: "chat", copy: {
        generic: "The action could not finish.", entitlementDenied: "Access denied.",
        transport: { requestTimedOut: "Request timed out.", serviceUnreachable: "Service unavailable.", retry: "Retry" },
      },
    });
    renderIntl(<ErrorNotice {...presentation} surface="chat" diagnosticsEnabled={false} />, { locale });
    expect(screen.getByRole("alert")).toHaveTextContent(expected);
    expect(screen.getByRole("alert")).not.toHaveTextContent(code);
    expect(screen.getByRole("alert")).not.toHaveTextContent("409");
    expect(screen.queryByRole("button")).toBeNull();
  });
});
