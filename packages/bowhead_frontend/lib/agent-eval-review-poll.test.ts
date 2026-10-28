import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./api-client", () => ({
  apiFetch: vi.fn(),
  readApiError: vi.fn(),
}));

import { apiFetch } from "./api-client";
import { REVIEW_POLL_INTERVAL_MS, reviewAgentEvalSuite } from "./agent-eval-alignment-api";

function reply(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

describe("Coach review polling", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.mocked(apiFetch).mockReset();
  });

  it("asks again while the server is still reviewing and returns the finished review", async () => {
    vi.useFakeTimers();
    vi.mocked(apiFetch)
      .mockResolvedValueOnce(reply({ status: "pending", gaps: [], duplicate_cases: [] }))
      .mockResolvedValueOnce(reply({ status: "pending", gaps: [], duplicate_cases: [] }))
      .mockResolvedValueOnce(reply({ status: "reviewed", gaps: [{ text: "Refunds", suggested_task: null }], duplicate_cases: [] }));
    const review = reviewAgentEvalSuite("osa_1", "aes_1", "aev_1");
    await vi.advanceTimersByTimeAsync(REVIEW_POLL_INTERVAL_MS * 2);
    await expect(review).resolves.toMatchObject({ status: "reviewed", gaps: [{ text: "Refunds", suggested_task: null }] });
    expect(apiFetch).toHaveBeenCalledTimes(3);
  });
});
