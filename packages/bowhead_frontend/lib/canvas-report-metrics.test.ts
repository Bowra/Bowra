import { describe, expect, it, vi } from "vitest";

import {
  bucketDocSize,
  bucketDuration,
  bucketSectionCount,
  trackReportSetContent,
} from "./canvas-report-metrics";

vi.mock("@/lib/metrics", () => ({
  track: vi.fn(),
}));

describe("canvas report metrics buckets", () => {
  it("buckets section count, doc size, and setContent duration", () => {
    expect(bucketSectionCount(0)).toBe("0");
    expect(bucketSectionCount(5)).toBe("1-5");
    expect(bucketSectionCount(16)).toBe("16-30");
    expect(bucketSectionCount(61)).toBe("60+");

    expect(bucketDocSize(49_999)).toBe("<50kb");
    expect(bucketDocSize(150_000)).toBe("150-500kb");
    expect(bucketDocSize(500_000)).toBe("500kb+");

    expect(bucketDuration(15.9)).toBe("<16ms");
    expect(bucketDuration(50)).toBe("50-150ms");
    expect(bucketDuration(150)).toBe("150ms+");
  });

  it("emits low-cardinality report perf labels", async () => {
    const { track } = await import("@/lib/metrics");
    trackReportSetContent({ sectionCount: 32, docBytes: 180_000, durationMs: 72 });
    expect(track).toHaveBeenCalledWith("bowhead_canvas_report_perf_total", {
      action: "set_content",
      sections: "31-60",
      doc_size: "150-500kb",
      duration: "50-150ms",
    });
  });
});
