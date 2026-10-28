import { describe, expect, it } from "vitest";

import { normalizeExportJobDTO } from "./api";

describe("normalizeExportJobDTO", () => {
  it("maps unknown export status to failed instead of polling forever", () => {
    const job = normalizeExportJobDTO({
      job_id: "job_1",
      canvas_id: "cnv_1",
      fmt: "pdf",
      status: "paused",
      created_at: "2026-06-05T00:00:00Z",
      updated_at: "2026-06-05T00:00:01Z",
    });

    expect(job.status).toBe("failed");
    // Keep internal explanations out of the UI: export notices use localized generic copy for failures without an explanation.
    expect(job.error).toBeNull();
  });

  it("normalizes the durable V2 snake_case DTO", () => {
    const job = normalizeExportJobDTO({
      job_id: "exp_v2",
      canvas_id: "cv_1",
      contract_version: 2,
      format: "pptx",
      status: "canceled",
      download_url: null,
      created_at: "2026-07-16T00:00:00Z",
      updated_at: "2026-07-16T00:00:01Z",
    });

    expect(job).toMatchObject({
      jobId: "exp_v2",
      canvasId: "cv_1",
      contractVersion: 2,
      fmt: "pptx",
      status: "canceled",
      url: null,
    });
  });

  it("keeps known running statuses unchanged", () => {
    const job = normalizeExportJobDTO({
      jobId: "job_2",
      canvasId: "cnv_1",
      fmt: "pptx",
      status: "running",
      createdAt: "2026-06-05T00:00:00Z",
      updatedAt: "2026-06-05T00:00:01Z",
    });

    expect(job.status).toBe("running");
    expect(job.error).toBeNull();
    expect(job.contractVersion).toBe(1);
  });

  it("normalizes explicit legacy contract_version", () => {
    const job = normalizeExportJobDTO({
      job_id: "job_3",
      canvas_id: "cnv_1",
      fmt: "xlsx",
      status: "done",
      contract_version: 1,
      created_at: "2026-06-05T00:00:00Z",
      updated_at: "2026-06-05T00:00:01Z",
    });

    expect(job.contractVersion).toBe(1);
  });

  it.each(["3", true, null, 4, 3.1])("fails malformed or unknown copy version %p closed", (version) => {
    const job = normalizeExportJobDTO({
      job_id: "job_bad_version",
      canvas_id: "cnv_1",
      fmt: "pdf",
      status: "done",
      contract_version: version,
      created_at: "2026-06-05T00:00:00Z",
      updated_at: "2026-06-05T00:00:01Z",
    });
    expect(job.contractVersion).toBe(1);
  });

  it("accepts only numeric exact copy contract 3", () => {
    const job = normalizeExportJobDTO({
      job_id: "job_current",
      canvas_id: "cnv_1",
      fmt: "pdf",
      status: "done",
      contract_version: 3,
      created_at: "2026-06-05T00:00:00Z",
      updated_at: "2026-06-05T00:00:01Z",
    });
    expect(job.contractVersion).toBe(3);
  });
});
