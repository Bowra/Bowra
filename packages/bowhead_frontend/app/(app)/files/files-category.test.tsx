/**
 * File categories are language-neutral codes: the filter dropdown and the drawer show localized
 * labels, but what goes to the backend is always the code.
 */
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import type { FileDTO } from "@/lib/api";

const api = vi.hoisted(() => ({
  listFiles: vi.fn(),
  fetchFilesUsage: vi.fn(),
  fetchFilePreview: vi.fn(),
  fetchFileUsages: vi.fn(),
  setFileCategory: vi.fn(),
}));

vi.mock("@/lib/api", async (orig) => ({ ...(await orig<Record<string, unknown>>()), ...api }));
vi.mock("@/lib/auth-api", () => ({ fetchMe: vi.fn(async () => ({ team_role: "member" })) }));
vi.mock("@/lib/use-space", () => ({ useSpaceSelection: () => ({ activeId: "ws_1", items: [] }) }));
vi.mock("@/components/shell/AppShell", () => ({ AppShell: ({ children }: { children: ReactNode }) => <>{children}</> }));
vi.mock("@/components/shell/Topbar", () => ({ Topbar: () => null }));
vi.mock("@/components/canvas/artifact-preview", () => ({ ArtifactPreview: () => null }));

import { FileDrawer } from "@/components/files/file-drawer";

import FilesPage from "./page";

const T = "2026-09-22T07:15:00Z";
const file = (extra: Partial<FileDTO> = {}): FileDTO => ({
  file_id: "f_1", filename: "plan.pdf", mime: "application/pdf", mime_family: "pdf", size: 1024, source: "upload",
  origin: "upload", created_at: T, last_used_at: T, deleted_at: null, category: "report", category_source: "llm",
  usage_count: 1, kind: "user_upload", trust: "trusted", in_kb: false, kb_promotable: true, kb: null,
  session_id: null, session_title: null, has_canvas_ref: false, retention_hint: null, url: "/api/v1/files/f_1",
  ...extra,
});

function renderDrawer(f: FileDTO) {
  return renderIntl(
    <FileDrawer
      file={f}
      tab="assets"
      onClose={vi.fn()}
      onDelete={vi.fn()}
      onRestore={vi.fn()}
      onGotoKb={vi.fn()}
      onUseInChat={vi.fn()}
      onAddToKb={vi.fn()}
      onRemoveFromKb={vi.fn()}
      onFileUpdated={vi.fn()}
    />,
  );
}

function categorySelect(): HTMLSelectElement {
  const selects = screen.getAllByRole("combobox", { name: "分类" });
  const found = selects.find((el) => el.classList.contains("bizsel"));
  if (!found) throw new Error("drawer category select not found");
  return found as HTMLSelectElement;
}

beforeEach(() => {
  document.documentElement.lang = "zh-CN";
  api.listFiles.mockResolvedValue({ items: [file()], total: 1, size_total_bytes: 1024 });
  api.fetchFilesUsage.mockResolvedValue(null);
  api.fetchFilePreview.mockResolvedValue({ family: "pdf", text: null, error: null });
  api.fetchFileUsages.mockResolvedValue({ usages: [] });
  api.setFileCategory.mockImplementation(async (_id: string, category: string) => file({ category, category_source: "user" }));
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  document.documentElement.lang = "";
});

describe("file category codes", () => {
  it("filter dropdown shows Chinese labels but sends the code", async () => {
    renderIntl(<FilesPage />);
    await screen.findByText("plan.pdf");
    // The page filter is a "分类：全部 ⌄" menu button (Polish Spec 2f), not a native select.
    fireEvent.click(screen.getByTestId("files-category-filter"));
    const menu = await screen.findByTestId("files-category-filter-menu");
    expect(within(menu).getAllByRole("menuitemradio").map((o) => o.textContent)).toEqual([
      "全部",
      "汇报材料",
      "数据明细",
      "参考资料",
      "会议纪要",
      "口径定义",
      "截图",
      "其他",
    ]);
    fireEvent.click(within(menu).getByRole("menuitemradio", { name: "会议纪要" }));
    await waitFor(() => expect(api.listFiles).toHaveBeenLastCalledWith(expect.objectContaining({ category: "meeting_notes" })));
    for (const [params] of api.listFiles.mock.calls) {
      expect(String((params as { category?: string }).category ?? "")).not.toMatch(/\p{Script=Han}/u);
    }
  });

  it("drawer shows the stored code's label and saves a code", async () => {
    renderDrawer(file());
    const select = categorySelect();
    expect(select.value).toBe("report");
    expect(select.selectedOptions[0].textContent).toBe("汇报材料");
    fireEvent.change(select, { target: { value: "dataset" } });
    await waitFor(() => expect(api.setFileCategory).toHaveBeenCalledWith("f_1", "dataset"));
  });

  it("drawer shows an unknown code verbatim instead of looking uncategorized", () => {
    renderDrawer(file({ category: "quarterly_review" }));
    const select = categorySelect();
    expect(select.value).toBe("quarterly_review");
    expect(select.selectedOptions[0].textContent).toBe("quarterly_review");
  });
});
