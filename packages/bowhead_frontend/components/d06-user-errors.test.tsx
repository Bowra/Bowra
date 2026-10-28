import "@testing-library/jest-dom/vitest";

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { FileDTO } from "@/lib/api";
import { ApiError } from "@/lib/api-client";
import messagesEn from "@/messages/en-US.json";
import messagesZh from "@/messages/zh-CN.json";

const mocks = vi.hoisted(() => ({
  preview: vi.fn(),
  usages: vi.fn(),
  category: vi.fn(),
  listDocTypes: vi.fn(),
}));

vi.mock("@/components/canvas/artifact-preview", () => ({
  ArtifactPreview: ({ error }: { error?: string | null }) => (
    <div role="alert">{error}</div>
  ),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    fetchFilePreview: (...args: unknown[]) => mocks.preview(...args),
    fetchFileUsages: (...args: unknown[]) => mocks.usages(...args),
    setFileCategory: (...args: unknown[]) => mocks.category(...args),
  };
});

vi.mock("@/lib/kb-knowledge-tables-api", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/lib/kb-knowledge-tables-api")>();
  return {
    ...actual,
    listDocTypesWithStats: (...args: unknown[]) => mocks.listDocTypes(...args),
  };
});

vi.mock("@/lib/space-storage", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/space-storage")>();
  return {
    ...actual,
    getActiveSpaceId: () => "ws_contract",
  };
});

vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams("tab=records"),
}));

vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => ({ activeId: "ws_contract", items: [], loading: false, error: null, refresh: () => undefined }),
}));

import { FileDrawer } from "./files/file-drawer";
import { MatrixTab } from "./knowledge/matrix/matrix-tab";

const file: FileDTO = {
  file_id: "art_contract",
  filename: "contract.csv",
  mime: "text/csv",
  mime_family: "spreadsheet",
  size: 1024,
  source: "upload",
  origin: "upload",
  created_at: "2026-07-28T00:00:00Z",
  last_used_at: null,
  deleted_at: null,
  category: null,
  category_source: null,
  usage_count: 0,
  kind: "user_upload",
  trust: "trusted",
  in_kb: false,
  kb_promotable: true,
  session_id: null,
  session_title: null,
  has_canvas_ref: false,
  retention_hint: null,
  url: "/api/v1/files/art_contract",
};

const locales = [
  {
    locale: "zh-CN",
    messages: messagesZh,
    preview:
      "无法生成文件预览，你仍可下载原文件或稍后重试。",
    records: "提取结果服务暂不可用，请稍后重试。",
  },
  {
    locale: "en-US",
    messages: messagesEn,
    preview:
      "A preview could not be generated. You can still download the original file or try again later.",
    records:
      "The extracted-results service is temporarily unavailable. Try again later.",
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
      timeZone="UTC"
    >
      {children}
    </NextIntlClientProvider>
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe.each(locales)("D06 user-visible errors ($locale)", (entry) => {
  it("shows a localized file-preview reason without backend prose or status", async () => {
    mocks.preview.mockResolvedValue({
      family: "spreadsheet",
      text: null,
      error: null,
      error_code: "file_preview_failed",
    });
    mocks.usages.mockResolvedValue({ usages: [] });

    render(
      <Provider entry={entry}>
        <FileDrawer
          file={file}
          tab="assets"
          onClose={vi.fn()}
          onDelete={vi.fn()}
          onRestore={vi.fn()}
          onGotoKb={vi.fn()}
          onUseInChat={vi.fn()}
          onAddToKb={vi.fn()}
          onRemoveFromKb={vi.fn()}
          onFileUpdated={vi.fn()}
        />
      </Provider>,
    );

    expect(await screen.findByText(entry.preview)).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("file_preview_failed");
    expect(document.body).not.toHaveTextContent("HTTP 200");
  });

  it("shows a localized records reason without raw API diagnostics", async () => {
    mocks.listDocTypes.mockRejectedValue(
      new ApiError(500, "RAW_SQLITE_PATH_AND_SCHEMA", {
        code: "kb_record_service_unavailable",
      }),
    );

    render(
      <Provider entry={entry}>
        <MatrixTab section="records" />
      </Provider>,
    );

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(entry.records);
    });
    expect(document.body).not.toHaveTextContent("RAW_SQLITE_PATH_AND_SCHEMA");
    expect(document.body).not.toHaveTextContent(
      "kb_record_service_unavailable",
    );
    expect(document.body).not.toHaveTextContent("HTTP 500");
  });
});
