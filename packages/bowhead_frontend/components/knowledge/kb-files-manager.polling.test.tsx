/**
 * List polling with a slow server—reproduced on 116 1.3.37, 2026-09-24.
 *
 * With files processing, poll every POLL_MS (3s); server responds in 4–9s. Old implementation incremented
 * load sequence on every poll, discarding the previous response as stale → no response applied, list froze forever;
 * post-upload/delete refreshes were also superseded by the next poll, requiring full-page refresh.
 *
 * Contracts:
 *   1. While a list request is in flight, polling sends no new request (no piling up on slow servers)
 *   2. Responses slower than polling interval still apply
 *   3. Before initial response, never show 0 files
 */

import "@testing-library/jest-dom/vitest";

import { act, cleanup, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api-client";
import type { KbFileDTO } from "@/lib/kb-api";
import messagesEn from "@/messages/en-US.json";

const mocks = vi.hoisted(() => ({
  fetchFiles: vi.fn(),
  extractStatus: vi.fn(),
  fetchMe: vi.fn(),
  listDocTypes: vi.fn(),
}));

vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));


vi.mock("@/lib/use-space", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/use-space")>();
  return {
    ...actual,
    useSpaceSelection: () => ({
      items: [],
      activeId: "ws_main",
      loading: false,
      error: null,
      setActiveId: vi.fn(),
      refresh: vi.fn(),
    }),
  };
});

vi.mock("@/lib/kb-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-api")>();
  return {
    ...actual,
    fetchKbFiles: (...args: unknown[]) => mocks.fetchFiles(...args),
    fetchExtractRowsStatusBatch: (...args: unknown[]) => mocks.extractStatus(...args),
  };
});

vi.mock("./kb-me", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./kb-me")>();
  return { ...actual, fetchKbMe: (...args: unknown[]) => mocks.fetchMe(...args) };
});

vi.mock("@/lib/kb-knowledge-tables-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-knowledge-tables-api")>();
  return { ...actual, listKnowledgeTables: (...args: unknown[]) => mocks.listDocTypes(...args) };
});

import { KbFilesManager } from "./kb-files-manager";

const POLL_MS = 3000;

function file(id: string, filename: string, parse_status: KbFileDTO["parse_status"]): KbFileDTO {
  return {
    file_id: id,
    artifact_id: `art_${id}`,
    filename,
    mime_type: "text/markdown",
    size_bytes: 100,
    scope: "personal",
    scope_id: "u_me",
    workspace_id: "ws_main",
    team_id: "team_main",
    uploaded_by: "u_me",
    uploaded_at: "2026-09-24T00:00:00Z",
    parse_status,
    parse_progress_pct: null,
    parse_layer: null,
    parse_error: null,
    parse_duration_ms: null,
    chunks_count: 0,
    cite_count: 0,
    last_cited_at: null,
    use_llm_marker: false,
    doc_type_id: null,
    summary_json: null,
    version: 1,
    soft_deleted_at: null,
  } as KbFileDTO;
}

type ListResult = { files: KbFileDTO[]; total: number; has_more: boolean };

/** Suspend list requests individually (not limit=1 counts); test decides when each returns. */
const pendingLists: Array<(r: ListResult) => void> = [];
const listCalls = () => mocks.fetchFiles.mock.calls.filter((c) => (c[0] as { limit?: number }).limit !== 1).length;

beforeEach(() => {
  // space-storage freezes the tab's Space context in sessionStorage on first read; reset it so an
  // earlier render cannot pin a stale (empty) Space for later tests in this file.
  window.sessionStorage.clear();
  vi.useFakeTimers();
  vi.clearAllMocks();
  pendingLists.length = 0;
  mocks.fetchMe.mockResolvedValue({ user_id: "u_me", workspace_id: "ws_main", team_id: "team_main", is_team_admin: false });
  mocks.listDocTypes.mockResolvedValue([]);
  mocks.fetchFiles.mockImplementation((params: { limit?: number }) => {
    if (params.limit === 1) return Promise.resolve({ files: [], total: 0, has_more: false });
    return new Promise<ListResult>((resolve) => pendingLists.push(resolve));
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

async function flush() {
  await act(async () => {
    await Promise.resolve();
  });
}

function renderManager() {
  return render(
    <NextIntlClientProvider locale="en-US" messages={messagesEn}>
      <KbFilesManager />
    </NextIntlClientProvider>,
  );
}

describe("KbFilesManager · 慢服务端下的轮询", () => {
  it("请求在途时轮询不发新请求，慢于轮询间隔的响应仍然落地", async () => {
    renderManager();
    await flush();
    expect(listCalls()).toBe(1);
    // Initial request pending: never show 0 files (FR4.2).
    expect(screen.queryByText(/files$/)).not.toBeInTheDocument();

    // First response: one file stuck processing → start polling.
    await act(async () => pendingLists[0]({ files: [file("f1", "stuck.pptx", "pending")], total: 1, has_more: false }));
    expect(screen.getByText("stuck.pptx")).toBeInTheDocument();

    // First poll starts with slow server: suspend across multiple polling intervals.
    await act(async () => vi.advanceTimersByTime(POLL_MS));
    expect(listCalls()).toBe(2);
    await act(async () => vi.advanceTimersByTime(POLL_MS * 3));
    expect(listCalls()).toBe(2);

    // Slow response includes newly uploaded file; it must apply.
    await act(async () =>
      pendingLists[1]({
        files: [file("f2", "new.md", "done"), file("f1", "stuck.pptx", "pending")],
        total: 2,
        has_more: false,
      }),
    );
    expect(screen.getByText("new.md")).toBeInTheDocument();

    // Polling resumes after in-flight request ends.
    await act(async () => vi.advanceTimersByTime(POLL_MS));
    expect(listCalls()).toBe(3);
  });

  it("抽屉跟随轮询结果更新状态（FR2.2）", async () => {
    renderManager();
    await flush();
    await act(async () => pendingLists[0]({ files: [file("f1", "stuck.pptx", "queued")], total: 1, has_more: false }));
    await act(async () => screen.getByText("stuck.pptx").click());
    const drawer = screen.getByTestId("kb-file-drawer");
    expect(drawer).toHaveTextContent("Queued");

    await act(async () => vi.advanceTimersByTime(POLL_MS));
    await act(async () => pendingLists[1]({ files: [file("f1", "stuck.pptx", "done")], total: 1, has_more: false }));
    expect(screen.getByTestId("kb-file-drawer")).toHaveTextContent("Available");
  });
});

describe("KbFilesManager · 行抽取任务进度轮询", () => {
  function extracting(): KbFileDTO {
    return { ...file("f1", "sheet.xlsx", "done"), extracted_rows_status: "running" } as KbFileDTO;
  }

  it("4xx（非空间管理员 403 等）停止轮询，不再每 3 秒重试", async () => {
    mocks.extractStatus.mockRejectedValue(new ApiError(403, "workspace_admin_required"));
    renderManager();
    await flush();
    await act(async () => pendingLists[0]({ files: [extracting()], total: 1, has_more: false }));
    await flush();
    expect(mocks.extractStatus).toHaveBeenCalledTimes(1);
    expect(mocks.extractStatus).toHaveBeenCalledWith(["f1"]);
    await act(async () => vi.advanceTimersByTime(POLL_MS * 4));
    expect(mocks.extractStatus).toHaveBeenCalledTimes(1);
    expect(screen.getByText("sheet.xlsx")).toBeInTheDocument();
  });

  it("5xx / 网络错误仍在下一拍重试", async () => {
    mocks.extractStatus.mockRejectedValue(new ApiError(503, "boom"));
    renderManager();
    await flush();
    await act(async () => pendingLists[0]({ files: [extracting()], total: 1, has_more: false }));
    await flush();
    expect(mocks.extractStatus).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTime(POLL_MS));
    await flush();
    expect(mocks.extractStatus).toHaveBeenCalledTimes(2);
  });
});

describe("fetchExtractRowsStatusBatch", () => {
  it("带当前空间头请求批量状态", async () => {
    vi.useRealTimers();
    const store = new Map<string, string>([["bowhead.space_id", "ws_main"]]);
    vi.stubGlobal("localStorage", { getItem: (k: string) => store.get(k) ?? null, setItem: vi.fn(), removeItem: vi.fn() });
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ jobs: {} }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    try {
      const actual = await vi.importActual<typeof import("@/lib/kb-api")>("@/lib/kb-api");
      await actual.fetchExtractRowsStatusBatch(["f1", "f2"]);
      const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
      expect(url).toContain("/api/v1/kb/files/_batch/extract_rows_status?file_ids=f1%2Cf2");
      expect(new Headers(init.headers).get("X-Space-Id")).toBe("ws_main");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("a failed background poll keeps the loaded rows and shows a one-line notice instead of the error card", async () => {
    renderManager();
    await flush();
    await act(async () => pendingLists[0]({ files: [file("f1", "stuck.pptx", "pending")], total: 1, has_more: false }));
    expect(screen.getByText("stuck.pptx")).toBeInTheDocument();

    mocks.fetchFiles.mockImplementation((params: { limit?: number }) => {
      if (params.limit === 1) return Promise.resolve({ files: [], total: 0, has_more: false });
      return Promise.reject(new ApiError(503, "upstream unavailable", {}));
    });
    await act(async () => vi.advanceTimersByTime(POLL_MS));
    await flush();

    expect(listCalls()).toBe(2);
    expect(screen.getByText("stuck.pptx")).toBeInTheDocument();
    expect(screen.queryByTestId("kb-error")).not.toBeInTheDocument();
    expect(screen.getByTestId("kb-refresh-error")).toHaveTextContent("The list couldn't be refreshed");
  });
});
