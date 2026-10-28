// Knowledge base UX cleanup P1 (interaction design §2, Final: 2b): /knowledge sections Files / Document Matrices /
// Extraction Results / Templates, drawn as PageFrame's underline tabs (Header, Language & Paging Spec §2, D5).
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

const mocks = vi.hoisted(() => ({
  search: "",
  push: vi.fn(),
  docTypes: vi.fn(),
  views: vi.fn(),
  matrices: vi.fn(),
  team: vi.fn(),
}));

vi.mock("@/lib/kb-capabilities-api", () => ({ fetchKbCapabilities: vi.fn().mockResolvedValue(null) }));
vi.mock("@/components/shell/AppShell", () => ({
  AppShell: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));
vi.mock("@/components/knowledge/kb-files-manager", () => ({
  KbFilesManager: ({ uploadRequest }: { uploadRequest?: number }) => (
    <div data-testid="files-manager" data-upload-request={String(uploadRequest)} />
  ),
}));
vi.mock("@/components/knowledge/matrix/matrix-tab", () => ({
  MatrixTab: ({ section }: { section?: string }) => <div data-testid="matrix-tab" data-section={section ?? ""} />,
}));
vi.mock("@/lib/auth/use-auth-redirect", () => ({
  useAuthRedirect: () => ({
    status: "authed",
    me: { capabilities: {} },
    isTeamAdmin: false,
    error: null,
  }),
}));
vi.mock("@/lib/kb-knowledge-tables-api", () => ({
  listDocTypesWithStats: (...args: unknown[]) => mocks.docTypes(...args),
}));
vi.mock("@/lib/kb-matrix/template-view", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-matrix/template-view")>();
  return { ...actual, listTemplateViews: (...args: unknown[]) => mocks.views(...args) };
});
vi.mock("@/lib/kb-matrix/snapshot-api", () => ({
  listMatrices: (...args: unknown[]) => mocks.matrices(...args),
}));
vi.mock("@/lib/kb-matrix/team-template-api", () => ({
  listTeamTemplates: (...args: unknown[]) => mocks.team(...args),
}));
vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => ({
    items: [{ id: "ws_1", name: "Space one", desc: "" }],
    activeId: "ws_1",
    loading: false,
    error: null,
    refresh: vi.fn(),
  }),
}));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: mocks.push, replace: vi.fn() }) }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(mocks.search) }));

import KnowledgePage from "@/app/(app)/knowledge/page";

function dt(id: string, kind: string, total: number) {
  return { doc_type_id: id, name: id, kind, fields: [], record_count: total };
}

describe("/knowledge · section tabs", () => {
  beforeEach(() => {
    mocks.search = "";
    mocks.push.mockReset();
    // 5 templates: 2 with records (→ 2 Extraction Results); 1 additional team template-library entry → 6 Templates
    mocks.docTypes.mockResolvedValue({
      items: [dt("seed_a", "seed", 0), dt("tpl_b", "template", 0), dt("m_c", "manual", 3), dt("m_d", "manual", 0), dt("g_e", "graduated", 1)],
      stats: {},
    });
    mocks.views.mockResolvedValue([]);
    mocks.matrices.mockResolvedValue([{ matrixId: "mx_1" }, { matrixId: "mx_2" }, { matrixId: "mx_3" }]);
    mocks.team.mockResolvedValue([{ entry_id: "ot_1" }]);
  });
  afterEach(cleanup);

  it("four link tabs; counts show placeholders while loading (never 0), then each counts its listed objects", async () => {
    let resolveDocTypes!: (v: unknown) => void;
    mocks.docTypes.mockReturnValueOnce(new Promise((r) => { resolveDocTypes = r; }));
    const { container } = renderIntl(<KnowledgePage />);
    const nav = await screen.findByTestId("kb-nav");
    expect(within(nav).getAllByRole("link").map((b) => b.textContent)).toEqual(["文件", "文档矩阵", "提取结果", "模板"]);
    expect(nav.querySelectorAll(".cnt--loading")).toHaveLength(3);
    expect(nav).not.toHaveTextContent(/\d/);

    resolveDocTypes({
      items: [dt("seed_a", "seed", 0), dt("tpl_b", "template", 0), dt("m_c", "manual", 3), dt("m_d", "manual", 0), dt("g_e", "graduated", 1)],
      stats: {},
    });
    await waitFor(() => expect(screen.getByTestId("kb-nav-matrix")).toHaveTextContent("文档矩阵3"));
    expect(screen.getByTestId("kb-nav-records")).toHaveTextContent("提取结果2");
    expect(screen.getByTestId("kb-nav-templates")).toHaveTextContent("模板6");
    expect(screen.getByTestId("kb-nav-files")).toHaveTextContent(/^文件$/);
    expect(container.querySelector(".ui-page-tabs .cnt--loading")).toBeNull();
  });

  it.each([
    ["tab=templates", "templates"],
    ["tab=matrix&view=templates", "templates"],
    ["tab=matrix&view=tpl_dt_1", "records"],
    ["tab=records", "records"],
    ["tab=matrix&view=mx_1", "matrix"],
  ])("深链 ?%s 落在「%s」视图", async (search, section) => {
    mocks.search = search;
    renderIntl(<KnowledgePage />);
    expect(await screen.findByTestId("matrix-tab")).toHaveAttribute("data-section", section);
    expect(screen.getByTestId(`kb-nav-${section}`)).toHaveAttribute("aria-current", "page");
  });

  it("矩阵或团队模板库接口不可用时计数照常：对应部分按空计", async () => {
    mocks.matrices.mockRejectedValue(new Error("404"));
    mocks.team.mockRejectedValue(new Error("404"));
    renderIntl(<KnowledgePage />);
    await waitFor(() => expect(screen.getByTestId("kb-nav-matrix")).toHaveTextContent("文档矩阵0"));
    expect(screen.getByTestId("kb-nav-records")).toHaveTextContent("提取结果2");
    expect(screen.getByTestId("kb-nav-templates")).toHaveTextContent("模板5");
  });

  it("页头上传：在其他视图点击 → 回到文件视图；在文件视图点击 → 请求打开上传弹窗", async () => {
    mocks.search = "tab=matrix&view=mx_1&file_id=f_1";
    renderIntl(<KnowledgePage />);
    fireEvent.click(await screen.findByTestId("kb-head-upload"));
    expect(mocks.push).toHaveBeenCalledWith("/knowledge?tab=files", { scroll: false });
    cleanup();

    mocks.search = "tab=files";
    renderIntl(<KnowledgePage />);
    const files = await screen.findByTestId("files-manager");
    expect(files).toHaveAttribute("data-upload-request", "0");
    fireEvent.click(screen.getByTestId("kb-head-upload"));
    expect(files).toHaveAttribute("data-upload-request", "1");
  });

  it("marks only the current section, and a plain click switches sections in place (keeping scope/space)", async () => {
    mocks.search = "tab=records&scope=team&file_id=f_1";
    renderIntl(<KnowledgePage />);
    const nav = await screen.findByTestId("kb-nav");
    expect(Array.from(nav.querySelectorAll("[aria-current='page']"))).toEqual([screen.getByTestId("kb-nav-records")]);
    const templates = screen.getByTestId("kb-nav-templates");
    expect(templates.getAttribute("href")).toBe("/knowledge?tab=templates&scope=team");
    fireEvent.click(templates);
    expect(mocks.push).toHaveBeenCalledWith("/knowledge?tab=templates&scope=team", { scroll: false });
  });
});
