/**
 * Deliverable file card (Chat Cards Spec §6, D10): line icon tile, extension first in the meta, two sibling
 * controls (open in Results, download), "Download failed" with a Retry button.
 */
import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

import { ChatInlineFile } from "./ChatInlineFile";

const state = vi.hoisted(() => ({
  results: true,
  download: { status: "idle" as "idle" | "downloading" | "done" | "error", pct: 0, download: vi.fn() },
  focus: vi.fn(),
}));
vi.mock("@/lib/use-results-workspace-capability", () => ({ useResultsWorkspaceCapabilitySnapshot: () => state.results }));
vi.mock("@/lib/use-file-download", () => ({ useFileDownload: () => state.download }));
vi.mock("@/lib/canvas-workspace-focus", () => ({ requestCanvasDataFocus: (...args: unknown[]) => state.focus(...args) }));

const DATA = {
  id: "art_deck",
  filename: "Q2 运营复盘.pptx",
  fileType: "pptx",
  downloadUrl: "/files/art_deck",
  sizeBytes: 3.4 * 1024 * 1024,
  slideCount: 12,
};

describe("ChatInlineFile", () => {
  it("shows a presentation line icon, the full file name and type · pages · size", () => {
    state.download = { status: "idle", pct: 0, download: vi.fn() };
    const { container } = renderIntl(<ChatInlineFile data={DATA} />);
    expect(container.querySelector(".ui-card__media svg")?.getAttribute("class")).toContain("lucide-presentation");
    expect(container.querySelector(".ui-card__title")?.textContent).toBe("Q2 运营复盘.pptx");
    expect(screen.getByRole("status").textContent).toBe("PPTX · 12 页 · 3.4 MB");
  });

  it("opens the result and downloads through two sibling buttons", () => {
    const download = vi.fn();
    state.download = { status: "idle", pct: 0, download };
    renderIntl(<ChatInlineFile data={DATA} />);
    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(2);
    expect(buttons[0].contains(buttons[1])).toBe(false);
    fireEvent.click(buttons[1]);
    expect(download).toHaveBeenCalledTimes(1);
    fireEvent.click(buttons[0]);
    expect(state.focus).toHaveBeenCalledWith("art_deck", { entryPoint: "chat" });
  });

  it("says only 'Download failed' and offers Retry as a button", () => {
    const download = vi.fn();
    state.download = { status: "error", pct: 0, download };
    renderIntl(<ChatInlineFile data={DATA} />);
    expect(screen.getByRole("status").textContent).toBe("下载失败");
    fireEvent.click(screen.getByText("重试"));
    expect(download).toHaveBeenCalledTimes(1);
  });

  it("draws download progress on the bottom edge", () => {
    state.download = { status: "downloading", pct: 46, download: vi.fn() };
    const { container } = renderIntl(<ChatInlineFile data={DATA} />);
    expect(container.querySelector<HTMLElement>(".ui-card")?.style.getPropertyValue("--card-progress")).toBe("46%");
    expect(screen.getByRole("status").textContent).toBe("下载中 · 46%");
  });

  it("without Results the whole card downloads", () => {
    state.results = false;
    const download = vi.fn();
    state.download = { status: "idle", pct: 0, download };
    renderIntl(<ChatInlineFile data={DATA} />);
    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(1);
    fireEvent.click(buttons[0]);
    expect(download).toHaveBeenCalledTimes(1);
    state.results = true;
  });
});
