/**
 * File versions (R2): the owner's versions list, newest first, each downloadable; a failed load offers Retry.
 */
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

import { FileVersionsDrawer } from "./file-versions-drawer";

const api = vi.hoisted(() => ({ list: vi.fn() }));
vi.mock("@/lib/results-api", () => ({ listResultVersions: (...args: unknown[]) => api.list(...args) }));

const ROWS = [
  { artifact_id: "art_v3", version_no: 3, filename: "report.html", mime: "text/html", size: 2048, created_at: "2026-11-13T08:00:00Z", latest: true },
  { artifact_id: "art_v2", version_no: 2, filename: "report.html", mime: "text/html", size: 1024, created_at: "2026-11-12T08:00:00Z", latest: false },
];

describe("FileVersionsDrawer", () => {
  it("lists the file's versions newest first with a download for each", async () => {
    api.list.mockResolvedValueOnce(ROWS);
    renderIntl(<FileVersionsDrawer sessionId="s_1" artifactId="art_v3" open onClose={vi.fn()} />);
    const rows = await screen.findAllByTestId("file-version-row");
    expect(api.list).toHaveBeenCalledWith(expect.objectContaining({ sessionId: "s_1", artifactId: "art_v3" }));
    expect(rows[0].textContent).toContain("第 3 版 · report.html");
    expect(rows[0].textContent).toContain("当前版本");
    expect(rows[1].textContent).not.toContain("当前版本");
    expect(screen.getByRole("link", { name: "下载第 2 版" }).getAttribute("href")).toBe("/api/v1/files/art_v2");
  });

  it("says the list failed to load and retries", async () => {
    api.list.mockRejectedValueOnce(new Error("down")).mockResolvedValueOnce(ROWS.slice(0, 1));
    renderIntl(<FileVersionsDrawer sessionId="s_1" artifactId="art_v3" open onClose={vi.fn()} />);
    expect((await screen.findByRole("alert")).textContent).toContain("版本列表加载失败");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(screen.getAllByTestId("file-version-row")).toHaveLength(1));
  });
});
