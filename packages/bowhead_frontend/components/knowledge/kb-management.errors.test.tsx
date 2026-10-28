import "@testing-library/jest-dom/vitest";

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api-client";
import type { KbDocTypeDTO } from "@/lib/kb-knowledge-tables-api";
import type { KbFileDTO } from "@/lib/kb-api";
import messagesEn from "@/messages/en-US.json";
import messagesZh from "@/messages/zh-CN.json";

const mocks = vi.hoisted(() => ({
  fetchFiles: vi.fn(),
  getFile: vi.fn(),
  search: vi.fn(),
  reparse: vi.fn(),
  extract: vi.fn(),
  bulkDelete: vi.fn(),
  bulkScope: vi.fn(),
  bulkBind: vi.fn(),
  download: vi.fn(),
  deleteFile: vi.fn(),
  fetchChunks: vi.fn(),
  fetchMe: vi.fn(),
  listDocTypes: vi.fn(),
}));

vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({
    push: vi.fn(),
    replace: vi.fn(),
  }),
}));


vi.mock("@/lib/kb-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-api")>();
  return {
    ...actual,
    fetchKbFiles: (...args: unknown[]) => mocks.fetchFiles(...args),
    getKbFile: (...args: unknown[]) => mocks.getFile(...args),
    searchKb: (...args: unknown[]) => mocks.search(...args),
    reparseKbFile: (...args: unknown[]) => mocks.reparse(...args),
    extractRowsKbFile: (...args: unknown[]) => mocks.extract(...args),
    bulkDeleteKbFiles: (...args: unknown[]) => mocks.bulkDelete(...args),
    bulkChangeKbFilesScope: (...args: unknown[]) => mocks.bulkScope(...args),
    bulkBindKbFilesDocType: (...args: unknown[]) => mocks.bulkBind(...args),
    downloadKbFileBlob: (...args: unknown[]) => mocks.download(...args),
    deleteKbFile: (...args: unknown[]) => mocks.deleteFile(...args),
    fetchKbFileChunks: (...args: unknown[]) => mocks.fetchChunks(...args),
  };
});

vi.mock("./kb-me", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./kb-me")>();
  return {
    ...actual,
    fetchKbMe: (...args: unknown[]) => mocks.fetchMe(...args),
  };
});

vi.mock("@/lib/kb-knowledge-tables-api", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/lib/kb-knowledge-tables-api")>();
  return {
    ...actual,
    listKnowledgeTables: (...args: unknown[]) => mocks.listDocTypes(...args),
  };
});

import { KbDocTypeModal } from "./kb-doctype-modal";
import { KbFileDrawer } from "./kb-file-drawer";
import { KbFilesManager } from "./kb-files-manager";
import { KbPreview } from "./kb-preview";
import { KbScopeModal } from "./kb-scope-modal";

const file: KbFileDTO = {
  file_id: "kbf_contract",
  artifact_id: "art_contract",
  filename: "合同.pdf",
  mime_type: "application/pdf",
  size_bytes: 1024,
  scope: "personal",
  scope_id: "u_me",
  workspace_id: "ws_main",
  team_id: "team_main",
  uploaded_by: "u_me",
  uploaded_by_name: "我",
  uploaded_at: "2026-07-28T00:00:00Z",
  parse_status: "failed",
  parse_progress_pct: null,
  parse_layer: null,
  parse_error: "RAW_PARSER_STACK_AND_PATH",
  parse_duration_ms: null,
  chunks_count: 0,
  cite_count: 0,
  last_cited_at: null,
  use_llm_marker: false,
  doc_type_id: null,
  summary_json: null,
  version: 1,
  soft_deleted_at: null,
  can_delete: true,
  can_change_scope: true,
};

const docType: KbDocTypeDTO = {
  doc_type_id: "dt_contract",
  workspace_id: "ws_main",
  name: "合同表",
  description: "",
  fields: [],
  record_count: 0,
  extractor_model: "qwen-plus",
  cross_verify_enabled: false,
  created_by: "u_me",
  created_at: "2026-07-28T00:00:00Z",
  updated_at: "2026-07-28T00:00:00Z",
  kind: "manual",
};

const locales = [
  {
    locale: "zh-CN",
    messages: messagesZh,
    access:
      "你没有权限执行此操作；如需访问，请联系空间管理员。",
    scope: "团队",
    next: "下一步",
    confirm: "确认迁移",
    scopeReason:
      "管理员移动其他成员的文件时，需要填写至少 10 个字符的原因。",
    apply: "应用",
    bind:
      "所选的不是提取表，不能用来提取文件，请重新选择一张提取表。",
    download: "下载",
    downloadError:
      "知识库服务暂不可用，请稍后重试；如问题持续，请联系管理员。",
    parseError:
      "文件解析失败，请重试解析；如问题持续，请联系管理员。",
    previewError:
      "无法打开这份文件。请检查访问权限或返回列表。",
  },
  {
    locale: "en-US",
    messages: messagesEn,
    access:
      "You do not have permission to do this. Contact a space administrator if you need access.",
    scope: "Team",
    next: "Next",
    confirm: "Confirm migration",
    scopeReason:
      "Administrators must provide a reason of at least 10 characters when moving another member's file.",
    apply: "Apply",
    bind:
      "The selection is not an extraction table and can't be used to extract files. Choose an extraction table.",
    download: "Download",
    downloadError:
      "The knowledge-base service is temporarily unavailable. Try again later, or contact an administrator if the problem continues.",
    parseError:
      "File parsing failed. Retry parsing, or contact an administrator if the problem continues.",
    previewError:
      "This file cannot be opened. Check your access or return to the list.",
  },
] as const;

function Provider({
  entry,
  children,
}: {
  entry: (typeof locales)[number];
  children: React.ReactNode;
}) {
  return (
    <NextIntlClientProvider
      locale={entry.locale}
      messages={entry.messages}
    >
      {children}
    </NextIntlClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.fetchMe.mockResolvedValue({
    user_id: "u_me",
    workspace_id: "ws_main",
    team_id: "team_main",
    is_team_admin: true,
  });
  mocks.getFile.mockResolvedValue(file);
  mocks.search.mockResolvedValue({
    chunks: [],
    total_candidates: 0,
    rerank_used: false,
    latency_ms: 1,
  });
  mocks.reparse.mockResolvedValue(file);
  mocks.extract.mockResolvedValue({ status: "queued" });
  mocks.bulkDelete.mockResolvedValue({
    results: [],
    succeeded: 0,
    failed: 0,
  });
  mocks.bulkScope.mockResolvedValue({
    results: [],
    succeeded: 0,
    failed: 0,
  });
  mocks.bulkBind.mockResolvedValue({
    results: [],
    succeeded: 0,
    failed: 0,
  });
  mocks.download.mockResolvedValue(undefined);
  mocks.deleteFile.mockResolvedValue(undefined);
  mocks.fetchChunks.mockResolvedValue({ chunks: [], total: 0 });
  mocks.listDocTypes.mockResolvedValue([docType]);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe.each(locales)("KB management errors — $locale", (entry) => {
  it("localizes the file-list error and ignores backend prose", async () => {
    mocks.fetchFiles.mockRejectedValue(
      new ApiError(403, "RAW_GRANT_POLICY_AND_RESOURCE_ID", {
        code: "resource_grant_denied",
      }),
    );

    render(
      <Provider entry={entry}>
        <KbFilesManager />
      </Provider>,
    );

    expect(await screen.findByTestId("kb-error")).toHaveTextContent(
      entry.access,
    );
    expect(document.body.textContent).not.toContain("resource_grant_denied");
    expect(document.body.textContent).not.toContain(
      "RAW_GRANT_POLICY_AND_RESOURCE_ID",
    );
  });

  it("localizes a HTTP 200 partial scope failure by error_code", async () => {
    mocks.bulkScope.mockResolvedValue({
      results: [
        {
          file_id: file.file_id,
          ok: false,
          error: "RAW_SCOPE_DATABASE_DIAGNOSTIC",
          error_code: "kb_scope_change_reason_required",
        },
      ],
      succeeded: 0,
      failed: 1,
    });

    render(
      <Provider entry={entry}>
        <KbScopeModal
          targets={[
            {
              file_id: file.file_id,
              filename: file.filename,
              scope: "personal",
            },
          ]}
          bulk={false}
          me={{
            user_id: "u_me",
            workspace_id: "ws_main",
            team_id: "team_main",
            is_team_admin: true,
          }}
          onClose={() => undefined}
          onMigrated={() => undefined}
          toast={() => undefined}
        />
      </Provider>,
    );

    fireEvent.click(screen.getByText(entry.scope));
    fireEvent.click(screen.getByRole("button", { name: entry.next }));
    fireEvent.click(screen.getByRole("button", { name: entry.confirm }));

    expect(await screen.findByText(entry.scopeReason)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain(
      "RAW_SCOPE_DATABASE_DIAGNOSTIC",
    );
    expect(document.body.textContent).not.toContain(
      "kb_scope_change_reason_required",
    );
  });

  it("localizes bind failure without rendering API message", async () => {
    mocks.bulkBind.mockRejectedValue(
      new ApiError(422, "RAW_DOC_TYPE_KIND", {
        code: "doc_type_not_bindable",
      }),
    );
    let toast = "";

    const { rerender } = render(
      <Provider entry={entry}>
        <div data-testid="bind-toast">{toast}</div>
        <KbDocTypeModal
          fileIds={[file.file_id]}
          workspaceId="ws_main"
          onClose={() => undefined}
          onApplied={() => undefined}
          toast={(_kind, content) => {
            toast = String(content);
          }}
        />
      </Provider>,
    );

    fireEvent.click(await screen.findByText(docType.name));
    fireEvent.click(screen.getByRole("button", { name: entry.apply }));
    await waitFor(() => expect(toast).toContain(entry.bind));
    rerender(
      <Provider entry={entry}>
        <div data-testid="bind-toast">{toast}</div>
      </Provider>,
    );
    expect(screen.getByTestId("bind-toast")).toHaveTextContent(entry.bind);
    expect(document.body.textContent).not.toContain("RAW_DOC_TYPE_KIND");
    expect(document.body.textContent).not.toContain("doc_type_not_bindable");
  });

  it("localizes download and stored parse failures", async () => {
    mocks.download.mockRejectedValue(
      new ApiError(503, "RAW_OBJECT_STORE_PATH", {
        code: "kb_service_unavailable",
      }),
    );

    render(
      <Provider entry={entry}>
        <KbFileDrawer
          file={file}
          onClose={() => undefined}
          onDeleted={() => undefined}
        />
      </Provider>,
    );

    expect(screen.getByText(entry.parseError)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain(
      "RAW_PARSER_STACK_AND_PATH",
    );
    fireEvent.click(
      screen.getByTestId("kb-download-link"),
    );
    expect(await screen.findByText(entry.downloadError)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("RAW_OBJECT_STORE_PATH");
  });

  it("localizes preview HTTP failure without raw code or status", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            detail: {
              error: {
                code: "kb_artifact_not_found",
                message: "RAW_ARTIFACT_BACKEND_PATH",
              },
            },
          }),
          {
            status: 404,
            headers: { "Content-Type": "application/json" },
          },
        ),
      ),
    );

    render(
      <Provider entry={entry}>
        <KbPreview
          file={{
            ...file,
            filename: "说明.md",
            mime_type: "text/markdown",
          }}
          onClose={() => undefined}
        />
      </Provider>,
    );

    expect(await screen.findByTestId("kb-preview-error")).toHaveTextContent(
      entry.previewError,
    );
    expect(document.body.textContent).not.toContain("kb_artifact_not_found");
    expect(document.body.textContent).not.toContain(
      "RAW_ARTIFACT_BACKEND_PATH",
    );
    expect(document.body.textContent).not.toContain("HTTP 404");
  });
});
