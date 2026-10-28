import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import type { KbFileDTO } from "@/lib/kb-api";
import { KbPreview } from "./kb-preview";

vi.mock("@/lib/md", () => ({ MarkdownWithCharts: ({ markdown }: { markdown: string }) => <p>{markdown}</p> }));
const file = { file_id: "kbf_retry", filename: "private-source.md", mime_type: "text/markdown" } as KbFileDTO;
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("makes a service failure compact, retries in place, and only offers actions known to work", async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ detail: "kb_service_unavailable" }), { status: 503 }))
    .mockResolvedValueOnce(new Response("Recovered content"));
  vi.stubGlobal("fetch", fetcher);
  renderIntl(<KbPreview file={file} onClose={vi.fn()} />);
  await screen.findByTestId("kb-preview-error");
  expect(screen.getByTestId("kb-preview-modal")).toHaveStyle({ height: "auto" });
  expect(screen.queryByRole("button", { name: /下载/ })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "重试预览" }));
  await screen.findByText("Recovered content");
  expect(fetcher).toHaveBeenCalledTimes(2);
});

it.each([403, 404])("shows neutral recovery for HTTP %s without stale filename or download", async (status) => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", { status })));
  renderIntl(<KbPreview file={file} onClose={vi.fn()} />);
  await screen.findByTestId("kb-preview-error");
  await waitFor(() => expect(screen.getByRole("dialog")).toHaveAccessibleName("无法打开文件"));
  expect(screen.queryByText(file.filename)).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /下载/ })).not.toBeInTheDocument();
});
