import { describe, expect, it } from "vitest";

import type { ArtifactItem } from "@/components/canvas/data-tab-files";
import {
  artifactItemFromResultDTO,
  deriveArtifactCapabilities,
  projectResultsDTO,
  projectResultsWorkspace,
  resolveSelectedResult,
} from "./results-workspace-model";
import type { ResultItemDTO } from "./results-api";

const labels = {
  reportEditable: "Editable report",
  reportReadonly: "Read-only report",
  codeRunning: "Running",
  codeTitle: "Code",
  codeLastRun: "Code",
  sourceUpload: "Uploaded by you",
  sourceGenerated: "AI generated",
  sourceExport: "Report export",
  sourceWeb: "Web",
  sourceSandbox: "Code run",
  sourceData: "Agent data",
  exportVersions: (count: number) => `${count} versions`,
  formatDate: (value: string) => value,
};

function artifact(overrides: Partial<ArtifactItem>): ArtifactItem {
  return {
    id: "art_data",
    filename: "data.csv",
    mime: "text/csv",
    size: 1,
    source: "bi_fetch_rows",
    created_at: "2026-08-07T00:00:00Z",
    preview_available: true,
    kind: "tool_data",
    ...overrides,
  };
}

describe("projectResultsWorkspace", () => {
  it("uses authorized renderer metadata for deep complete-list artifacts", () => {
    const dto: ResultItemDTO = {
      key: "artifact:art_deep",
      object_type: "artifact",
      title: "深层表格.xlsx",
      primary_group: "evidence",
      secondary_group: "data",
      viewer_profile: "table",
      status: "ready",
      route: { type: "artifact", artifact_id: "art_deep", default_mode: "preview" },
      capabilities: ["download", "view_details", "view_raw"],
      relation_roles: ["report_detail_table"],
      execution: null,
      display_context: {
        source_role: "data_query",
        output_role: "report_detail_table",
        execution_label: null,
        occurred_at: "2026-08-25T00:00:00+00:00",
        duplicate_ordinal: null,
        duplicate_count: null,
      },
      position: 101,
      version: null,
      artifact_metadata: {
        mime: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size: 8192,
        source: "bi_fetch_rows",
        kind: "tool_data",
        origin: "query",
        trust: "trusted",
        created_at: "2026-08-25T00:00:00+00:00",
        producer_kind: "bi_fetch_rows",
      },
    };

    expect(artifactItemFromResultDTO(dto)).toMatchObject({
      id: "art_deep",
      mime: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      size: 8192,
    });
    expect(projectResultsDTO([dto], labels)[0]).toMatchObject({
      key: "artifact:art_deep",
      viewerProfile: "table",
      route: { type: "artifact", artifactId: "art_deep", mode: "preview" },
      artifactItem: { id: "art_deep", size: 8192 },
    });
  });

  it("sorts Report first and evidence into data, code, and input groups", () => {
    const items = projectResultsWorkspace({
      artifacts: [
        artifact({}),
        artifact({ id: "art_input", filename: "input.csv", kind: "user_upload", source: "upload" }),
        artifact({ id: "art_delivery", filename: "brief.html", mime: "text/html", kind: "deliverable" }),
      ],
      report: { canvasId: "canvas_1", title: "Q3 report", editable: true },
      code: { sessionId: "session_1", available: true, running: false },
      labels,
      readOnly: false,
      reportOnly: false,
    });

    expect(items.map((item) => [item.key, item.primaryGroup, item.secondaryGroup])).toEqual([
      ["report:canvas_1", "deliverables", null],
      ["artifact:art_delivery", "deliverables", null],
      ["artifact:art_data", "evidence", "data"],
      ["code:session_1", "evidence", "code"],
      ["artifact:art_input", "evidence", "input"],
    ]);
  });

  it("folds authorized export jobs and keeps old deep links selected", () => {
    const items = projectResultsWorkspace({
      artifacts: [
        artifact({ id: "art_pdf_new", filename: "report.pdf", mime: "application/pdf", kind: "deliverable" }),
        artifact({ id: "art_pdf_old", filename: "report.pdf", mime: "application/pdf", kind: "deliverable" }),
      ],
      exportGroups: [{
        key: "canvas_1:pdf",
        canvasId: "canvas_1",
        fmt: "pdf",
        latestJobId: "job_running",
        latestStatus: "running",
        currentArtifactId: "art_pdf_new",
        exportJobCount: 3,
        versions: [
          { artifactId: "art_pdf_new", jobId: "job_new", createdAt: "2026-08-07T02:00:00Z", updatedAt: "" },
          { artifactId: "art_pdf_old", jobId: "job_old", createdAt: "2026-08-07T01:00:00Z", updatedAt: "" },
        ],
      }],
      labels,
      readOnly: false,
      reportOnly: false,
    });

    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      key: "export:canvas_1:pdf",
      stableId: "art_pdf_new",
      status: "processing",
      memberStableIds: ["art_pdf_new", "art_pdf_old"],
      version: { availableVersionCount: 2, exportJobCount: 3 },
    });
    expect(resolveSelectedResult(items, {
      type: "artifact",
      artifactId: "art_pdf_old",
    })?.key).toBe("export:canvas_1:pdf");
    expect(items[0]?.route).toEqual({
      type: "artifact",
      artifactId: "art_pdf_new",
      mode: "preview",
    });
  });

  it("does not expose owner-only Code or Files capabilities in report-only mode", () => {
    const items = projectResultsWorkspace({
      artifacts: [artifact({ id: "art_shared" })],
      code: { sessionId: "session_1", available: true, running: true },
      labels,
      readOnly: true,
      reportOnly: true,
    });

    expect(items.some((item) => item.objectType === "code")).toBe(false);
    expect(items[0]?.capabilities.has("manage_in_files")).toBe(false);
    expect(items[0]?.capabilities.has("reference_in_chat")).toBe(false);
  });

  it("derives artifact operations by role instead of applying one generic menu", () => {
    const deliverable = deriveArtifactCapabilities({
      item: artifact({ kind: "deliverable", filename: "brief.html", mime: "text/html" }),
      readOnly: false,
      reportOnly: false,
    });
    const data = deriveArtifactCapabilities({
      item: artifact({ kind: "tool_data" }),
      readOnly: false,
      reportOnly: false,
    });
    const input = deriveArtifactCapabilities({
      item: artifact({ kind: "user_upload", source: "upload" }),
      readOnly: false,
      reportOnly: false,
    });
    const unsupported = deriveArtifactCapabilities({
      item: artifact({ filename: "archive.bin", mime: "application/octet-stream" }),
      readOnly: false,
      reportOnly: false,
    });

    expect([...deliverable]).toEqual(expect.arrayContaining([
      "download", "reference_in_chat", "manage_in_files", "view_raw", "view_details",
    ]));
    expect(data.has("manage_in_files")).toBe(false);
    expect(data.has("reference_in_chat")).toBe(true);
    expect(input.has("manage_in_files")).toBe(true);
    expect(unsupported.has("reference_in_chat")).toBe(false);
    expect(unsupported.has("feedback_unsupported")).toBe(true);
  });

  it("fails closed for unreadable objects and removes owner-only actions in report-only scope", () => {
    const item = artifact({
      filename: "archive.bin",
      mime: "application/octet-stream",
    });
    expect(deriveArtifactCapabilities({
      item,
      readOnly: false,
      reportOnly: false,
      readable: false,
    }).size).toBe(0);

    const shared = deriveArtifactCapabilities({
      item,
      readOnly: true,
      reportOnly: true,
    });
    expect([...shared]).toEqual(["download", "view_details"]);
  });

  it.each([
    ["image deliverable", "image_generate", "deliverable", "image/png", "deliverables", null, "image"],
    ["canvas PDF", "canvas_export_skill", "deliverable", "application/pdf", "deliverables", null, "document"],
    ["consultant deck", "consultant_deck", "deliverable", "application/vnd.openxmlformats-officedocument.presentationml.presentation", "deliverables", null, "document"],
    ["sandbox data", "python_sandbox", "sandbox_output", "text/csv", "evidence", "data", "table"],
    ["BI rows", "bi_fetch_rows", "tool_data", "text/csv", "evidence", "data", "table"],
    ["web clip", "web_fetch", "web_clip", "text/markdown", "evidence", "data", "text"],
    ["user input", "upload", "user_upload", "application/pdf", "evidence", "input", "document"],
  ] as const)("keeps the audited %s producer in one semantic group", (
    _name,
    source,
    kind,
    mime,
    primaryGroup,
    secondaryGroup,
    viewerProfile,
  ) => {
    const [item] = projectResultsWorkspace({
      artifacts: [artifact({ source, kind, mime, filename: "fixture" })],
      labels,
      readOnly: false,
      reportOnly: false,
    });

    expect(item).toMatchObject({ primaryGroup, secondaryGroup, viewerProfile });
  });

  it("uses precise origin metadata and carries stable turn grouping", () => {
    const items = projectResultsWorkspace({
      artifacts: [
        artifact({
          id: "art_export",
          filename: "report.pdf",
          source: "canvas_export_skill",
          origin: "export",
          kind: "deliverable",
          created_by_turn: "turn_export",
        }),
        artifact({
          id: "art_image",
          filename: "figure.png",
          source: "image_generate",
          origin: "compute",
          kind: "deliverable",
          mime: "image/png",
        }),
      ],
      labels,
      readOnly: false,
      reportOnly: false,
    });

    expect(items.find((item) => item.key === "artifact:art_export")).toMatchObject({
      metadata: "Report export",
      runId: "turn_export",
    });
    expect(items.find((item) => item.key === "artifact:art_image")?.metadata)
      .toBe("AI generated");
  });
});
