"use client";

import { track } from "@/lib/metrics";

export type CountBucket = "0" | "1-5" | "6-15" | "16-30" | "31-60" | "60+";
export type SizeBucket = "<50kb" | "50-150kb" | "150-500kb" | "500kb+";
export type DurationBucket = "<16ms" | "16-50ms" | "50-150ms" | "150ms+";

export function bucketSectionCount(count: number): CountBucket {
  if (count <= 0) return "0";
  if (count <= 5) return "1-5";
  if (count <= 15) return "6-15";
  if (count <= 30) return "16-30";
  if (count <= 60) return "31-60";
  return "60+";
}

export function bucketDocSize(bytes: number): SizeBucket {
  if (bytes < 50_000) return "<50kb";
  if (bytes < 150_000) return "50-150kb";
  if (bytes < 500_000) return "150-500kb";
  return "500kb+";
}

export function bucketDuration(ms: number): DurationBucket {
  if (ms < 16) return "<16ms";
  if (ms < 50) return "16-50ms";
  if (ms < 150) return "50-150ms";
  return "150ms+";
}

export function trackReportSetContent({
  sectionCount,
  docBytes,
  durationMs,
}: {
  sectionCount: number;
  docBytes: number;
  durationMs: number;
}): void {
  track("bowhead_canvas_report_perf_total", {
    action: "set_content",
    sections: bucketSectionCount(sectionCount),
    doc_size: bucketDocSize(docBytes),
    duration: bucketDuration(durationMs),
  });
}
