import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  screen,
  within,
} from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";

const resolveClarificationMock = vi.fn();
vi.mock("@/lib/api", () => ({
  MAX_DIRECTIVE_CHARS: 4000,
  resolveClarification: (...args: unknown[]) =>
    resolveClarificationMock(...args),
}));

import { FormulaConfirmCard } from "./formula-confirm-card";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.sessionStorage.clear();
});

function renderCard(locale: "zh-CN" | "en-US" = "zh-CN") {
  return renderIntl(
    <FormulaConfirmCard
      workspaceId="ws_1"
      requestId="clar_formula"
      token="EBITDA"
      proposedFormula="EBITDA = revenue - cost"
    />,
    { locale },
  );
}

describe("FormulaConfirmCard errors", () => {
  it("does not render a raw Error.message and shows the generic submit failure", async () => {
    // Since fe927ae3e only observed browser network failures count as transport errors.
    resolveClarificationMock.mockRejectedValue(
      new Error(
        "browser public URL is not registered; password=raw-secret",
      ),
    );
    renderCard();

    fireEvent.click(
      screen.getByRole("button", { name: "确认 / 修改" }),
    );

    expect(
      await screen.findByText("没能提交回答，你的选择已保留。"),
    ).toBeTruthy();
    expect(document.body.textContent).not.toContain("raw-secret");
  });

  it("exposes only transport facts for a browser network failure", async () => {
    resolveClarificationMock.mockRejectedValue(new TypeError("Failed to fetch"));
    renderCard();

    fireEvent.click(
      screen.getByRole("button", { name: "确认 / 修改" }),
    );

    expect(
      await screen.findByText(
        "暂时无法连接 Bowhead，请检查网络连接后重试。",
      ),
    ).toBeTruthy();
    const details = screen.getByTestId("error-diagnostics");
    fireEvent.click(within(details).getByText("查看技术详情"));
    expect(within(details).getByText("不可用（未收到 HTTP 响应）"))
      .toBeTruthy();
  });

  it("shows the localized catalog message for a matching publisher", async () => {
    const message =
      "The submitted content does not meet the safety or format requirements. Revise it and try again.";
    resolveClarificationMock.mockRejectedValue(
      new ApiError(422, message, {
        code: "clarification_input_rejected",
        diagnostics: {
          schemaVersion: 1,
          disclosure: "authenticated",
          status: 422,
          code: "clarification_input_rejected",
          diagnosticCode: "rule_clarification_failure",
          safeDetail:
            "phase=validate_input; failure_class=validation",
          retryable: false,
        },
      }),
    );
    renderCard("en-US");

    fireEvent.click(
      screen.getByRole("button", { name: "Confirm / Edit" }),
    );

    expect(await screen.findByText(message)).toBeTruthy();
    expect(
      within(screen.getByTestId("error-diagnostics")).getByText(
        "clarification_input_rejected",
      ),
    ).toBeTruthy();
  });
});
