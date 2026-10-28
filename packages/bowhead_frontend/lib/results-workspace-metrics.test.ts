import { beforeEach, describe, expect, it, vi } from "vitest";

const { track } = vi.hoisted(() => ({ track: vi.fn() }));

vi.mock("./metrics", () => ({ track }));

import { trackResultsWorkspace } from "./results-workspace-metrics";

describe("Results Workspace metrics", () => {
  beforeEach(() => track.mockReset());

  it("emits only the complete bounded dimensions", () => {
    trackResultsWorkspace({
      action: "result_selected",
      resultType: "artifact",
      group: "evidence",
      entryPoint: "navigator",
      mode: "preview",
    });

    expect(track).toHaveBeenCalledWith("bowhead_results_workspace_total", {
      action: "result_selected",
      result_type: "artifact",
      group: "evidence",
      entry_point: "navigator",
      mode: "preview",
      error_class: "none",
    });
    expect(JSON.stringify(track.mock.calls)).not.toContain("artifact_id");
    expect(JSON.stringify(track.mock.calls)).not.toContain("filename");
  });
});
