/**
 * Deliverable file card (Chat Cards Spec §6, D10): line icon tile, extension first in the meta, two sibling
 * controls (open in Results, download), "Download failed" with a Retry button.
 */
import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

import { resetFileVersions } from "@/lib/file-version-registry";

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

  it("file versions: opens and downloads the newest version, with a secondary download of the delivered one", () => {
    const latestDownload = vi.fn();
    state.download = { status: "idle", pct: 0, download: latestDownload };
    renderIntl(
      <ChatInlineFile
        data={{
          ...DATA,
          id: "art_v1",
          filename: "report.html",
          fileType: "html",
          versionNo: 1,
          latest: { id: "art_v3", versionNo: 3, filename: "report-v3.html", downloadUrl: "/files/art_v3", sizeBytes: 2048 },
        }}
      />,
    );
    expect(screen.getByText("report-v3.html")).toBeTruthy();
    expect(screen.getByRole("status").textContent).toBe("已更新到第 3 版 · HTML · 2.0 KB");
    fireEvent.click(screen.getAllByRole("button")[0]);
    expect(state.focus).toHaveBeenCalledWith("art_v3", { entryPoint: "chat" });
    expect(screen.getByTestId("chat-inline-file-this-version").textContent).toContain("下载第 1 版");
  });

  it("file versions: a cleaned-up delivered version says so instead of offering it", () => {
    state.download = { status: "idle", pct: 0, download: vi.fn() };
    renderIntl(
      <ChatInlineFile
        data={{
          ...DATA,
          versionNo: 2,
          pruned: true,
          latest: { id: "art_v5", versionNo: 5, filename: DATA.filename, downloadUrl: "/files/art_v5" },
        }}
      />,
    );
    expect(screen.queryByTestId("chat-inline-file-this-version")).toBeNull();
    expect(screen.getByText("第 2 版已清理，只保留最近 3 个版本")).toBeTruthy();
  });

  it("file versions: an older card learns a newer version delivered while the page is open (116 recheck N6)", () => {
    resetFileVersions();
    state.download = { status: "idle", pct: 0, download: vi.fn() };
    const first = { ...DATA, id: "art_v1", filename: "报告.html", fileType: "html", lineage: { root: "art_v1", versionNo: 1 } };
    const second = { ...DATA, id: "art_v2", filename: "报告.html", fileType: "html", downloadUrl: "/files/art_v2", lineage: { root: "art_v1", versionNo: 2 } };
    renderIntl(
      <>
        <ChatInlineFile data={first} />
        <ChatInlineFile data={second} />
      </>,
    );
    const cards = screen.getAllByTestId("chat-inline-file");
    expect(cards[0].textContent).toContain("已更新到第 2 版");
    expect(cards[0].textContent).toContain("下载第 1 版");
    expect(cards[1].textContent).not.toContain("已更新到");
  });
});
