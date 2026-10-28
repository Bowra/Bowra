import { describe, expect, it } from "vitest";

import type { ExportJobDTO } from "@/lib/api";
import { projectResultExportVersions } from "./results-export-versions";

function job(overrides: Partial<ExportJobDTO>): ExportJobDTO {
  return {
    jobId: "job_1",
    canvasId: "canvas_1",
    contractVersion: 2,
    fmt: "pdf",
    status: "done",
    url: null,
    artifactId: "art_1",
    filename: "report.pdf",
    size: 10,
    error: null,
    createdAt: "2026-08-07T00:00:00Z",
    updatedAt: "2026-08-07T00:00:00Z",
    ...overrides,
  };
}

describe("projectResultExportVersions", () => {
  it("keeps the latest successful artifact while a newer job is running", () => {
    const [group] = projectResultExportVersions([
      job({ jobId: "job_running", status: "running", artifactId: null,
        createdAt: "2026-08-07T02:00:00Z" }),
      job({ jobId: "job_done", artifactId: "art_current",
        createdAt: "2026-08-07T01:00:00Z" }),
    ]);

    expect(group).toMatchObject({
      key: "canvas_1:pdf",
      latestJobId: "job_running",
      latestStatus: "running",
      currentArtifactId: "art_current",
    });
    expect(group?.versions.map((version) => version.artifactId)).toEqual([
      "art_current",
    ]);
  });

  it("preserves all successful versions and does not merge formats", () => {
    const groups = projectResultExportVersions([
      job({ jobId: "job_pdf_2", artifactId: "art_pdf_2",
        createdAt: "2026-08-07T02:00:00Z" }),
      job({ jobId: "job_pdf_1", artifactId: "art_pdf_1",
        createdAt: "2026-08-07T01:00:00Z" }),
      job({ jobId: "job_pptx", fmt: "pptx", artifactId: "art_pptx" }),
    ]);

    expect(groups).toHaveLength(2);
    expect(groups.find((group) => group.fmt === "pdf")?.versions).toEqual([
      expect.objectContaining({ artifactId: "art_pdf_2" }),
      expect.objectContaining({ artifactId: "art_pdf_1" }),
    ]);
    expect(groups.find((group) => group.fmt === "pptx")?.currentArtifactId)
      .toBe("art_pptx");
  });
});
