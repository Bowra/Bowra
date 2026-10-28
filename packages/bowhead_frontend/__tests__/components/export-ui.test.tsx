import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ExportMenu,
  ExportsDrawer,
  ExportToast,
} from "@/components/canvas/export-ui";
import { renderIntl } from "@/__tests__/intl-render";
import type { ExportJobDTO } from "@/lib/api";
import type { ExportState } from "@/lib/use-canvas-export";
import { useMenu } from "@/components/ui/menu";

/** The caller (the report workbench) owns the export menu trigger; this is a minimal host with the same structure. */
function ExportMenuHost({
  onPick = () => {},
  onOpenHistory = () => {},
}: {
  onPick?: (fmt: string) => void;
  onOpenHistory?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const menu = useMenu({ open, onOpenChange: setOpen, placement: "bottom-end" });
  return (
    <>
      <button type="button" ref={menu.refs.setReference} {...menu.getReferenceProps()}>
        Export
      </button>
      <p>Outside</p>
      <ExportMenu
        menu={menu}
        busy={false}
        onPick={onPick}
        onOpenHistory={onOpenHistory}
      />
    </>
  );
}

const apiMocks = vi.hoisted(() => ({
  listExportJobs: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  exportJobDownloadUrl: (job: ExportJobDTO) => job.url,
  listExportJobs: apiMocks.listExportJobs,
}));

function exportJob(overrides: Partial<ExportJobDTO> = {}): ExportJobDTO {
  return {
    jobId: "exp_legacy",
    canvasId: "cv_1",
    fmt: "pptx",
    status: "done",
    url: "/api/v1/files/art_1",
    artifactId: "art_1",
    filename: "report.pptx",
    size: 2048,
    error: null,
    contractVersion: 1,
    createdAt: "2026-07-16T00:00:00Z",
    updatedAt: "2026-07-16T00:00:01Z",
    ...overrides,
  };
}

beforeEach(() => {
  apiMocks.listExportJobs.mockReset();
  apiMocks.listExportJobs.mockResolvedValue([]);
});

afterEach(cleanup);

describe("export menu behaviour", () => {
  it("opens on the keyboard into the first format and moves with the arrow keys", async () => {
    renderIntl(<ExportMenuHost />);
    fireEvent.click(screen.getByRole("button", { name: "Export" }), { detail: 0 });

    const items = await screen.findAllByRole("menuitem");
    await waitFor(() => expect(document.activeElement).toBe(items[0]));
    fireEvent.keyDown(screen.getByRole("menu"), { key: "ArrowDown" });
    expect(document.activeElement).toBe(items[1]);
    fireEvent.keyDown(screen.getByRole("menu"), { key: "End" });
    expect(document.activeElement?.textContent).toContain("导出记录");
  });

  it("closes on Escape and returns focus to the trigger", async () => {
    renderIntl(<ExportMenuHost />);
    fireEvent.click(screen.getByRole("button", { name: "Export" }), { detail: 0 });
    const first = (await screen.findAllByRole("menuitem"))[0];
    await waitFor(() => expect(document.activeElement).toBe(first));

    fireEvent.keyDown(first, { key: "Escape" });

    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Export" })));
  });

  it("closes on a press outside", async () => {
    renderIntl(<ExportMenuHost />);
    fireEvent.click(screen.getByRole("button", { name: "Export" }));
    expect(screen.getByRole("menu")).toBeTruthy();

    fireEvent.pointerDown(screen.getByText("Outside"));

    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });

  it("hands focus back to the trigger before running the picked export", async () => {
    let focusedDuringPick: Element | null = null;
    const onOpenHistory = vi.fn(() => {
      focusedDuringPick = document.activeElement;
    });
    renderIntl(<ExportMenuHost onOpenHistory={onOpenHistory} />);
    fireEvent.click(screen.getByRole("button", { name: "Export" }));

    fireEvent.click(screen.getByRole("menuitem", { name: /导出记录/ }));

    expect(onOpenHistory).toHaveBeenCalledTimes(1);
    expect(focusedDuringPick).toBe(screen.getByRole("button", { name: "Export" }));
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });

  it.each([
    ["pdf", "PDF 文档"],
    ["pptx", "PowerPoint 演示文稿"],
    ["docx", "Word 文档"],
    ["xlsx", "Excel 表格"],
  ])("starts the selected %s report export and closes the menu", async (format, label) => {
    const onPick = vi.fn();
    renderIntl(<ExportMenuHost onPick={onPick} />);
    fireEvent.click(screen.getByRole("button", { name: "Export" }));
    fireEvent.click(screen.getByRole("menuitem", { name: new RegExp(label) }));
    expect(onPick).toHaveBeenCalledExactlyOnceWith(format);
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });
});

describe("legacy Canvas export honesty", () => {
  it("does not promise full coverage when runtime V2 is unavailable", () => {
    const view = renderIntl(<ExportMenuHost />);
    fireEvent.click(view.getByRole("button", { name: "Export" }));

    expect(view.getByText("PowerPoint 演示文稿")).toBeTruthy();
    expect(view.getByText("按报告内容导出；复杂图表可能转为数据表")).toBeTruthy();
  });

  it("shows the legacy validation warning on a completed job", () => {
    const state: ExportState = {
      job: exportJob(),
      timedOut: false,
      startError: null,
      lastArgs: { canvasId: "cv_1", fmt: "pptx" },
    };
    const view = renderIntl(
      <ExportToast
        state={state}
        onDismiss={() => {}}
        onRetry={() => {}}
        onRetryDefaultTheme={() => {}}
        onOpenHistory={() => {}}
      />,
    );

    expect(view.getByText("旧版导出，可能包含过时的可信性表述")).toBeTruthy();
    expect(view.getByText("重新生成")).toBeTruthy();
  });

  it("describes a polling deadline as background processing rather than failure", () => {
    const state: ExportState = {
      job: exportJob({ status: "running", url: null, artifactId: null }),
      timedOut: true,
      startError: null,
      lastArgs: { canvasId: "cv_1", fmt: "pptx" },
    };
    const view = renderIntl(
      <ExportToast
        state={state}
        onDismiss={() => {}}
        onRetry={() => {}}
        onRetryDefaultTheme={() => {}}
        onOpenHistory={() => {}}
      />,
    );

    expect(view.getByText("仍在后台处理")).toBeTruthy();
    expect(view.queryByText(/超时/)).toBeNull();
  });

  it("keeps long-running jobs honest and exposes historical failure details", async () => {
    apiMocks.listExportJobs.mockResolvedValue([
      exportJob({ status: "failed", url: null, artifactId: null, error: "renderer exploded" }),
    ]);
    const view = renderIntl(
      <ExportsDrawer canvasId="cv_1" open onClose={() => {}} />,
    );

    await waitFor(() => {
      expect(view.getByText(/renderer exploded/)).toBeTruthy();
      expect(view.queryByText("旧版导出，可能包含过时的可信性表述")).toBeNull();
    });
  });

  it("closes the export history drawer on Escape", async () => {
    apiMocks.listExportJobs.mockResolvedValue([]);
    const onClose = vi.fn();
    const view = renderIntl(
      <ExportsDrawer canvasId="cv_1" open onClose={onClose} />,
    );
    const drawer = view.getByRole("dialog");
    expect(drawer.getAttribute("data-testid")).toBe("exports-drawer");
    await waitFor(() => expect(apiMocks.listExportJobs).toHaveBeenCalled());

    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("treats only completed exact contract 3 as current", async () => {
    apiMocks.listExportJobs.mockResolvedValue([
      exportJob({ jobId: "exp_current", contractVersion: 3 }),
      exportJob({ jobId: "exp_unknown", contractVersion: 4 }),
    ]);
    const view = renderIntl(
      <ExportsDrawer canvasId="cv_1" open onClose={() => {}} />,
    );
    await waitFor(() => {
      expect(view.getAllByText("旧版导出，可能包含过时的可信性表述")).toHaveLength(1);
    });
  });
});
