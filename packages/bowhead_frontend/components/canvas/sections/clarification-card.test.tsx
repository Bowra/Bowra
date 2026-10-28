import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api-client";
import { ClarificationCard } from "./clarification-card";

const apiMocks = vi.hoisted(() => ({
  cancelClarificationDirective: vi.fn(),
  resolveClarification: vi.fn(),
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

vi.mock("@/lib/api", () => ({
  cancelClarificationDirective: apiMocks.cancelClarificationDirective,
  resolveClarification: apiMocks.resolveClarification,
  MAX_DIRECTIVE_CHARS: 4000,
  MAX_EDITED_SUB_DESC_CHARS: 1000,
  MAX_EDITED_SUB_TITLE_CHARS: 200,
  MAX_EDITED_SUBS_COUNT: 10,
}));

describe("Canvas ClarificationCard skip", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("token skip sends the backend skip payload", async () => {
    apiMocks.resolveClarification.mockResolvedValue({ ok: true });
    render(
      <ClarificationCard
        workspaceId="ws_1"
        requestId="clar_token"
        token="GMV"
        kind="token"
        candidates={[{ value: "gross_merchandise_value", label: "GMV" }]}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "btn.skip" }));

    await waitFor(() => {
      expect(apiMocks.resolveClarification).toHaveBeenCalledWith("clar_token", {
        skip: true,
      });
    });
  });

  it("deprecated canvas kinds do not render the new skip button", () => {
    render(
      <ClarificationCard
        workspaceId="ws_1"
        requestId="clar_concept"
        token="metric"
        kind="concept_resolution"
        candidates={[
          { value: "concept_1", label: "Concept 1" },
          { value: "concept_2", label: "Concept 2" },
        ]}
      />,
    );

    expect(screen.queryByRole("button", { name: "btn.skip" })).toBeNull();
  });

  it("ambiguous_turn none-of-these uses backend skip instead of local-only hide", async () => {
    apiMocks.resolveClarification.mockResolvedValue({ ok: true });
    render(
      <ClarificationCard
        workspaceId="ws_1"
        requestId="clar_ambiguous"
        token=""
        kind="ambiguous_turn"
        candidates={[
          { value: "sales", label: "Sales" },
          { value: "profit", label: "Profit" },
        ]}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "btn.reject_all" }));

    await waitFor(() => {
      expect(apiMocks.resolveClarification).toHaveBeenCalledWith("clar_ambiguous", {
        skip: true,
      });
    });
  });

  it("shows the generic submit failure for an unclassified Error without rendering Error.message", async () => {
    // Since fe927ae3e only observed browser network failures count as transport errors.
    apiMocks.resolveClarification.mockRejectedValue(
      new Error("browser public URL is not registered: raw-secret"),
    );
    render(
      <ClarificationCard
        workspaceId="ws_1"
        requestId="clar_unclassified"
        token="GMV"
        kind="token"
        candidates={[{ value: "gmv", label: "GMV" }]}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "GMV" }));

    await waitFor(() => {
      expect(screen.getByTestId("error-notice")).toHaveTextContent("submit_failed");
    });
    expect(document.body.textContent).not.toContain("raw-secret");
  });

  it("shows a progressive transport error for a browser network failure", async () => {
    apiMocks.resolveClarification.mockRejectedValue(new TypeError("Failed to fetch"));
    render(
      <ClarificationCard
        workspaceId="ws_1"
        requestId="clar_transport"
        token="GMV"
        kind="token"
        candidates={[{ value: "gmv", label: "GMV" }]}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "GMV" }));

    await waitFor(() => {
      expect(screen.getByTestId("error-notice")).toHaveTextContent(
        "transport.serviceUnreachable",
      );
    });
    expect(screen.getByTestId("error-diagnostics")).toBeInTheDocument();
  });

  it("shows a published localized API error with expandable diagnostics", async () => {
    apiMocks.resolveClarification.mockRejectedValue(
      new ApiError(422, "请修改不符合要求的内容后重试。", {
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
      }),
    );
    render(
      <ClarificationCard
        workspaceId="ws_1"
        requestId="clar_policy"
        token="GMV"
        kind="token"
        candidates={[{ value: "gmv", label: "GMV" }]}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "GMV" }));

    await waitFor(() => {
      expect(screen.getByTestId("error-notice")).toHaveTextContent(
        "请修改不符合要求的内容后重试。",
      );
    });
    expect(screen.getByTestId("error-diagnostics")).toBeInTheDocument();
  });
});
