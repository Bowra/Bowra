import { cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

const api = vi.hoisted(() => ({
  getAccessRequestStatus: vi.fn(),
  requestAccess: vi.fn(),
  withdrawAccessRequest: vi.fn(),
}));
vi.mock("@/lib/access-requests-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/access-requests-api")>()),
  ...api,
}));

import { ReportUnavailable } from "./report-unavailable";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ReportUnavailable", () => {
  it("shows the request panel with the server-backed request state", async () => {
    api.getAccessRequestStatus.mockResolvedValue({ state: "none", request: null, retry_after: null });
    renderIntl(<ReportUnavailable sessionId="s1" />);
    expect(await screen.findByLabelText("申请理由（选填）")).toBeTruthy();
    expect(api.getAccessRequestStatus).toHaveBeenCalledWith("report", "s1");
  });

  it("shows only the informational placeholder when requests are off", () => {
    renderIntl(<ReportUnavailable sessionId="s1" canRequest={false} />);
    expect(screen.queryByTestId("access-request-panel")).toBeNull();
    expect(screen.getByText("这份报告还没有分享给你。你可以向所有者申请访问。")).toBeTruthy();
    expect(api.getAccessRequestStatus).not.toHaveBeenCalled();
  });
});
