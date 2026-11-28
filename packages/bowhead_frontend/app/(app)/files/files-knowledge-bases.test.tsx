/**
 * Knowledge bases (FEATURE_KNOWLEDGE_BASES) on the Files page: "加入知识库" adds to the chosen knowledge base,
 * `?add_to_kb=<id>` preselects it without opening the dialog, and the links open the knowledge base page.
 * Switch off: today's call and links.
 */
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { SPACES_FIXTURE, directoryFixture } from "@/components/knowledge/bases/kb-target-picker.fixtures";
import type { FileDTO } from "@/lib/api";
import messagesZh from "@/messages/zh-CN.json";

const api = vi.hoisted(() => ({
  listFiles: vi.fn(),
  fetchFilesUsage: vi.fn(),
  promoteFileToKb: vi.fn(),
}));
const mocks = vi.hoisted(() => ({ knowledgeBases: true }));

vi.mock("@/lib/api", async (orig) => ({ ...(await orig<Record<string, unknown>>()), ...api }));
vi.mock("@/lib/auth-api", () => ({ fetchMe: vi.fn(async () => ({ team_role: "member" })) }));
vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => ({ activeId: "ws_1", items: SPACES_FIXTURE, loading: false, error: null, setActiveId: () => {}, refresh: () => {} }),
}));
vi.mock("@/lib/kb-ingestion-readiness", () => ({ useKbIngestionReadiness: () => ({ state: "ready", retry: vi.fn() }) }));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useOptionalAuthSession: () => ({ me: { capabilities: { knowledge_bases: mocks.knowledgeBases } } }),
}));
vi.mock("@/components/knowledge/bases/use-knowledge-bases", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/knowledge/bases/use-knowledge-bases")>()),
  useKnowledgeBaseDirectory: () => ({ directory: directoryFixture(), loading: false, error: null, refresh: vi.fn() }),
}));
vi.mock("@/components/shell/AppShell", () => ({ AppShell: ({ children }: { children: ReactNode }) => <>{children}</> }));
vi.mock("@/components/shell/Topbar", () => ({ Topbar: () => null }));
vi.mock("@/components/files/file-drawer", () => ({ FileDrawer: () => <div data-testid="file-preview" /> }));

import FilesPage from "./page";

const T = "2026-09-22T07:15:00Z";
const file = (id: string, name: string, extra: Partial<FileDTO> = {}): FileDTO => ({
  file_id: id, filename: name, mime: "application/pdf", mime_family: "pdf", size: 1024, source: "upload",
  origin: "upload", created_at: T, last_used_at: T, deleted_at: null, category: null, category_source: null,
  usage_count: 1, kind: "upload", trust: "user", in_kb: false, kb_promotable: true, kb: null,
  session_id: null, session_title: null, has_canvas_ref: false, retention_hint: null, url: `/api/v1/files/${id}`,
  ...extra,
});
const PLAIN = file("f_plain", "九月销售.pdf");
const IN_KB = file("f_kb", "经营手册.pdf", {
  in_kb: true,
  kb: { file_id: "kbf_7", scope: "space", workspace_id: "ws_1", parse_status: "done", parse_progress_pct: null, parse_stuck: false, parse_error_code: null },
});

beforeEach(() => {
  mocks.knowledgeBases = true;
  window.history.replaceState(null, "", "/files/?add_to_kb=kb_ops");
  api.listFiles.mockImplementation(async (params: { include_deleted?: boolean }) => {
    const items = params.include_deleted ? [] : [PLAIN, IN_KB];
    return { items, total: items.length, size_total_bytes: 2048 };
  });
  api.fetchFilesUsage.mockResolvedValue(null);
  api.promoteFileToKb.mockResolvedValue({ file_id: "kbf_new", workspace_id: "ws_1" });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.history.replaceState(null, "", "/");
});

describe("Files page · knowledge bases on", () => {
  it("preselects ?add_to_kb without opening the dialog, adds to that knowledge base and links to it", async () => {
    renderIntl(<FilesPage />);
    await screen.findByText("九月销售.pdf");
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByTestId("kb-add"));
    expect(screen.getByTestId("kb-target-kb_ops")).toHaveAttribute("aria-checked", "true");
    fireEvent.click(screen.getByTestId("kb-add-confirm"));
    await waitFor(() => expect(api.promoteFileToKb).toHaveBeenCalledWith("f_plain", "space", "kb_ops"));
    expect(await screen.findByRole("link", { name: "查看处理进度" })).toHaveAttribute("href", "/knowledge/base/?file_id=kbf_new&space=ws_1");
  });

  it("the Knowledge column opens the knowledge base page with the file selected", async () => {
    renderIntl(<FilesPage />);
    await screen.findByText("经营手册.pdf");
    expect(screen.getByRole("link", { name: "在知识库中查看" })).toHaveAttribute("href", "/knowledge/base/?file_id=kbf_7&space=ws_1");
  });

  it("explains knowledge-base refusals after adding", async () => {
    api.promoteFileToKb.mockRejectedValue(new Error("knowledge_base_scope_mismatch"));
    renderIntl(<FilesPage />);
    await screen.findByText("九月销售.pdf");
    fireEvent.click(screen.getByTestId("kb-add"));
    fireEvent.click(screen.getByTestId("kb-add-confirm"));
    expect(await screen.findByText(messagesZh.knowledge_base.errors.knowledge_base_scope_mismatch)).toBeTruthy();
  });
});

describe("Files page · knowledge bases off", () => {
  it("keeps today's call and links", async () => {
    mocks.knowledgeBases = false;
    renderIntl(<FilesPage />);
    await screen.findByText("九月销售.pdf");
    expect(screen.getByRole("link", { name: "在知识库中查看" })).toHaveAttribute("href", "/knowledge/?scope=space&space=ws_1&file_id=kbf_7");
    fireEvent.click(screen.getByTestId("kb-add"));
    expect(screen.queryByTestId("kb-target-picker")).toBeNull();
    fireEvent.click(screen.getByTestId("kb-add-confirm"));
    await waitFor(() => expect(api.promoteFileToKb).toHaveBeenCalledWith("f_plain", "personal"));
    expect(api.promoteFileToKb.mock.calls[0]).toHaveLength(2);
  });
});
