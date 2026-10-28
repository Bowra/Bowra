/**
 * The export request carries the exporting user's legend-hidden series (user decision 2026-10-01, option b) —
 * and only when something is hidden, so other exports keep their request (and idempotency) hash.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { startExportJobV2, type StartExportJobV2Input } from "./api";

const INPUT: StartExportJobV2Input = {
  format: "pdf",
  profile: "full_report",
  locale: "zh-CN",
  timezone: "Asia/Shanghai",
  expected_projection_token: `sha256:${"a".repeat(64)}`,
  expected_surface_token: `sha256:${"b".repeat(64)}`,
  client_surface_receipt_sha256: `sha256:${"c".repeat(64)}`,
  client_visual_receipts: [],
  theme: {} as StartExportJobV2Input["theme"],
};

async function sentOptions(input: StartExportJobV2Input): Promise<Record<string, unknown>> {
  const fetchMock = vi.fn(async () => new Response(JSON.stringify({ jobId: "exp_1", status: "queued" }), { status: 202 }));
  vi.stubGlobal("fetch", fetchMock);
  await startExportJobV2("cv_1", input, "key").catch(() => undefined);
  const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1];
  return JSON.parse(String(init.body)).options;
}

afterEach(() => vi.unstubAllGlobals());

describe("startExportJobV2 chart_hidden_series", () => {
  it("sends the hidden series inside options", async () => {
    expect(await sentOptions({ ...INPUT, chart_hidden_series: { sec_a: ["rate"] } })).toEqual({
      include_appendix: true,
      chart_hidden_series: { sec_a: ["rate"] },
    });
  });

  it("omits the key when nothing is hidden", async () => {
    expect(await sentOptions({ ...INPUT, chart_hidden_series: {} })).toEqual({ include_appendix: true });
    expect(await sentOptions(INPUT)).toEqual({ include_appendix: true });
  });
});
