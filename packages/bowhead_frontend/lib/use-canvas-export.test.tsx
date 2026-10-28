import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useCanvasExport } from "./use-canvas-export";
import { chartStyleExportThemeFixture } from "@/__tests__/chart-style-export-fixture";

const apiMocks = vi.hoisted(() => ({
  fetchExportJob: vi.fn(),
  fetchExportJobV2: vi.fn(),
  startExportJob: vi.fn(),
  startExportJobV2: vi.fn(),
}));

vi.mock("./api", () => ({
  fetchExportJob: apiMocks.fetchExportJob,
  fetchExportJobV2: apiMocks.fetchExportJobV2,
  startExportJob: apiMocks.startExportJob,
  startExportJobV2: apiMocks.startExportJobV2,
}));

beforeEach(() => {
  apiMocks.fetchExportJob.mockReset();
  apiMocks.fetchExportJobV2.mockReset();
  apiMocks.startExportJob.mockReset();
  apiMocks.startExportJobV2.mockReset();
  apiMocks.startExportJob.mockRejectedValue(new Error("expected test failure"));
  apiMocks.startExportJobV2.mockRejectedValue(
    new Error("expected V2 test failure"),
  );
});

describe("useCanvasExport retry contract", () => {
  it("does not poll a V2 job that starts already done with copy contract 3", async () => {
    apiMocks.startExportJobV2.mockResolvedValueOnce({
      jobId: "exp_done",
      canvasId: "cv_1",
      contractVersion: 3,
      fmt: "pdf",
      status: "done",
      url: "/api/v1/files/art_done",
      artifactId: "art_done",
      filename: "report.pdf",
      size: 42,
      error: null,
      createdAt: "2026-08-01T00:00:00Z",
      updatedAt: "2026-08-01T00:00:01Z",
    });
    const { result } = renderHook(() => useCanvasExport());

    await act(async () => {
      await result.current.start("cv_1", "pdf", undefined, {
        format: "pdf",
        profile: "full_report",
        locale: "zh-CN",
        timezone: "Asia/Singapore",
        expected_projection_token: `sha256:${"a".repeat(64)}`,
        expected_surface_token: `sha256:${"b".repeat(64)}`,
        client_surface_receipt_sha256: `sha256:${"c".repeat(64)}`,
        client_visual_receipts: [],
        theme: chartStyleExportThemeFixture(),
        include_appendix: true,
      }, "done-key");
    });

    expect(result.current.state.job?.contractVersion).toBe(3);
    expect(apiMocks.fetchExportJob).not.toHaveBeenCalled();
    expect(apiMocks.fetchExportJobV2).not.toHaveBeenCalled();
  });

  it("drops the template artifact when retrying with the default theme", async () => {
    const { result } = renderHook(() => useCanvasExport());

    await act(async () => {
      await result.current.start("cv_1", "pptx", {
        mode: "clone",
        template_artifact_id: "art_user_template",
      });
    });

    act(() => {
      result.current.retryWithDefaultTheme();
    });

    await waitFor(() => expect(apiMocks.startExportJob).toHaveBeenCalledTimes(2));
    expect(apiMocks.startExportJob.mock.calls[1]).toEqual([
      "cv_1",
      "pptx",
      { mode: "default" },
    ]);
  });

  it("sends the prepared surface receipt to the plural V2 endpoint", async () => {
    const { result } = renderHook(() => useCanvasExport());
    const v2Request = {
      format: "pdf" as const,
      profile: "full_report" as const,
      locale: "zh-CN" as const,
      timezone: "Asia/Singapore",
      expected_projection_token: `sha256:${"a".repeat(64)}`,
      expected_surface_token: `sha256:${"b".repeat(64)}`,
      client_surface_receipt_sha256: `sha256:${"c".repeat(64)}`,
      client_visual_receipts: [],
      theme: chartStyleExportThemeFixture(),
      include_appendix: true,
    };

    await act(async () => {
      await result.current.start(
        "cv_1",
        "pdf",
        undefined,
        v2Request,
        "canvas-export-test",
      );
    });

    expect(apiMocks.startExportJobV2).toHaveBeenCalledWith(
      "cv_1",
      v2Request,
      "canvas-export-test",
    );
    expect(apiMocks.startExportJob).not.toHaveBeenCalled();
  });
});
