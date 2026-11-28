// Knowledge bases (FEATURE_KNOWLEDGE_BASES, PRD §4.1 Q1): the Knowledge page with the switch on, and proof that the
// switch off keeps today's page (tabs, labels, the Files manager, upload and file deep links).
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { directoryFixture } from "@/components/knowledge/bases/kb-directory.fixtures";
import type { KnowledgeBaseDirectory } from "@/lib/knowledge-bases-api";

const mocks = vi.hoisted(() => ({
  search: "",
  caps: {} as Record<string, unknown>,
  push: vi.fn(),
  replace: vi.fn(),
  docTypes: vi.fn(),
  matrices: vi.fn(),
  team: vi.fn(),
  toast: vi.fn(),
  notifyChanged: vi.fn(),
  dir: { directory: null as KnowledgeBaseDirectory | null, loading: false, error: null as Error | null, refresh: vi.fn() },
  dirEnabled: [] as boolean[],
}));

vi.mock("@/lib/kb-capabilities-api", () => ({ fetchKbCapabilities: vi.fn().mockResolvedValue(null) }));
vi.mock("@/components/shell/AppShell", () => ({
  AppShell: ({ children, mobileActions }: { children: React.ReactNode; mobileActions?: React.ReactNode }) => (
    <div>
      <div data-testid="mobile-actions">{mobileActions}</div>
      {children}
    </div>
  ),
}));
vi.mock("@/components/knowledge/kb-files-manager", () => ({
  KbFilesManager: ({ uploadRequest, initialFileId }: { uploadRequest?: number; initialFileId?: string | null }) => (
    <div data-testid="files-manager" data-upload-request={String(uploadRequest)} data-file-id={initialFileId ?? ""} />
  ),
}));
vi.mock("@/components/knowledge/kb-index-notice", () => ({ KbIndexNotice: () => <div data-testid="index-notice" /> }));
vi.mock("@/components/knowledge/matrix/matrix-tab", () => ({
  MatrixTab: ({ section, knowledgeBasesLayout }: { section?: string; knowledgeBasesLayout?: boolean }) => (
    <div data-testid="matrix-tab" data-section={section ?? ""} data-kb-layout={String(Boolean(knowledgeBasesLayout))} />
  ),
}));
vi.mock("@/components/knowledge/bases/kb-directory", () => ({ KbDirectory: () => <div data-testid="kb-directory-stub" /> }));
vi.mock("@/components/knowledge/bases/kb-create-dialog", () => ({
  KbCreateDialog: ({ onCreated, onClose }: { onCreated: (kb: { id: string }) => void; onClose: () => void }) => (
    <div role="dialog" aria-label="create-stub">
      <button type="button" onClick={() => onCreated({ id: "kb_new" })}>create-it</button>
      <button type="button" onClick={onClose}>close-create</button>
    </div>
  ),
}));
vi.mock("@/components/knowledge/kb-upload-modal", () => ({
  KbUploadModal: ({
    defaultScope,
    defaultKnowledgeBaseId,
    onUploaded,
  }: {
    defaultScope: string;
    defaultKnowledgeBaseId?: string | null;
    onUploaded: (dto: Record<string, unknown>) => void;
  }) => (
    <div role="dialog" aria-label="upload-stub" data-scope={defaultScope} data-kb={defaultKnowledgeBaseId ?? ""}>
      <button
        type="button"
        onClick={() => onUploaded({ file_id: "f_new", filename: "排班规则.md", scope: "space", scope_id: "ws_1", knowledge_base_id: "kb_ops" })}
      >
        upload-it
      </button>
    </div>
  ),
}));
vi.mock("@/components/knowledge/bases/use-knowledge-bases", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/knowledge/bases/use-knowledge-bases")>();
  return {
    ...actual,
    useKnowledgeBaseDirectory: ({ enabled = true }: { enabled?: boolean } = {}) => {
      mocks.dirEnabled.push(enabled);
      return enabled ? mocks.dir : { directory: null, loading: true, error: null, refresh: vi.fn() };
    },
  };
});
vi.mock("@/lib/knowledge-bases-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/knowledge-bases-api")>();
  return { ...actual, notifyKnowledgeBasesChanged: () => mocks.notifyChanged() };
});
vi.mock("@/lib/use-toast", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/use-toast")>();
  return { ...actual, toast: (...args: unknown[]) => mocks.toast(...args) };
});
vi.mock("@/lib/auth/use-auth-redirect", () => ({
  useAuthRedirect: () => ({
    status: "authed",
    me: { id: "u_1", team_id: "team_1", capabilities: mocks.caps },
    isTeamAdmin: false,
    error: null,
  }),
}));
vi.mock("@/lib/kb-knowledge-tables-api", () => ({
  listDocTypesWithStats: (...args: unknown[]) => mocks.docTypes(...args),
}));
vi.mock("@/lib/kb-matrix/snapshot-api", () => ({
  listMatrices: (...args: unknown[]) => mocks.matrices(...args),
}));
vi.mock("@/lib/kb-matrix/team-template-api", () => ({
  listTeamTemplates: (...args: unknown[]) => mocks.team(...args),
}));
vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => ({
    items: [{ id: "ws_1", name: "商超零售场景", desc: "", role: "member" }],
    activeId: "ws_1",
    loading: false,
    error: null,
    refresh: vi.fn(),
  }),
}));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: mocks.push, replace: mocks.replace }) }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(mocks.search) }));

import KnowledgePage from "@/app/(app)/knowledge/page";

function dt(id: string, kind: string, total: number) {
  return { doc_type_id: id, name: id, kind, fields: [], record_count: total };
}

beforeEach(() => {
  mocks.search = "";
  mocks.caps = { knowledge_bases: true };
  mocks.push.mockReset();
  mocks.replace.mockReset();
  mocks.toast.mockReset();
  mocks.notifyChanged.mockReset();
  mocks.dirEnabled = [];
  mocks.dir = { directory: directoryFixture(), loading: false, error: null, refresh: vi.fn() };
  // 2 extraction tables (one with results) + a graduated template with results → 3 extraction-table objects;
  // seed + template + graduated + 1 Team library entry → 4 matrix templates.
  mocks.docTypes.mockResolvedValue({
    items: [dt("seed_a", "seed", 0), dt("tpl_b", "template", 0), dt("m_c", "manual", 3), dt("m_d", "manual", 0), dt("g_e", "graduated", 1)],
    stats: {},
  });
  mocks.matrices.mockResolvedValue([{ matrixId: "mx_1" }, { matrixId: "mx_2" }]);
  mocks.team.mockResolvedValue([{ entry_id: "ot_1" }]);
});
afterEach(cleanup);

describe("/knowledge · knowledge bases switch on", () => {
  it("tabs are Knowledge bases / Document matrix / Extraction tables / Templates, counted by what each lists", async () => {
    renderIntl(<KnowledgePage />);
    const nav = await screen.findByTestId("kb-nav");
    expect(within(nav).getAllByRole("link").map((link) => link.querySelector("span")?.textContent)).toEqual([
      "知识库",
      "文档矩阵",
      "提取表",
      "模板",
    ]);
    expect(screen.getByTestId("kb-nav-bases")).toHaveAttribute("aria-current", "page");
    expect(screen.getByTestId("kb-nav-bases")).toHaveTextContent("知识库8");
    await waitFor(() => expect(screen.getByTestId("kb-nav-matrix")).toHaveTextContent("文档矩阵2"));
    expect(screen.getByTestId("kb-nav-records")).toHaveTextContent("提取表3");
    expect(screen.getByTestId("kb-nav-templates")).toHaveTextContent("模板4");
    expect(screen.queryByTestId("kb-nav-files")).not.toBeInTheDocument();
    expect(screen.getByTestId("kb-directory-stub")).toBeInTheDocument();
    expect(screen.queryByTestId("files-manager")).not.toBeInTheDocument();
  });

  it("a failed directory read shows no count on its tab", async () => {
    mocks.dir = { directory: null, loading: false, error: new Error("x"), refresh: vi.fn() };
    renderIntl(<KnowledgePage />);
    expect(await screen.findByTestId("kb-nav-bases")).toHaveTextContent(/^知识库$/);
  });

  it.each([
    ["", "bases"],
    ["tab=files", "bases"],
    ["tab=bases", "bases"],
    ["tab=records", "records"],
    ["tab=matrix&view=tpl_dt_1", "records"],
    ["tab=templates", "templates"],
    ["tab=matrix&view=templates", "templates"],
    ["tab=matrix&view=mx_1", "matrix"],
  ])("?%s opens the %s tab", async (search, section) => {
    mocks.search = search;
    renderIntl(<KnowledgePage />);
    expect(await screen.findByTestId(`kb-nav-${section}`)).toHaveAttribute("aria-current", "page");
    if (section === "bases") {
      expect(screen.getByTestId("kb-directory-stub")).toBeInTheDocument();
    } else {
      const tab = screen.getByTestId("matrix-tab");
      expect(tab).toHaveAttribute("data-section", section);
      // Extraction tables merge results with definitions; Templates keep matrix templates only (MatrixTab layout).
      expect(tab).toHaveAttribute("data-kb-layout", "true");
    }
  });

  it("the directory header offers Add files and New knowledge base, keeps the index notice, and describes the directory", async () => {
    renderIntl(<KnowledgePage />);
    await screen.findByTestId("kb-directory-stub");
    expect(screen.getByText("按知识库整理资料，并设置谁能使用。对话和智能体会检索你能使用的知识库。")).toBeInTheDocument();
    expect(screen.getByTestId("kb-head-upload")).toHaveTextContent("添加文件");
    expect(screen.getByTestId("kb-head-upload")).toHaveClass("settings-button--primary");
    expect(screen.getByTestId("kb-head-new-kb")).toHaveTextContent("新建知识库");
    expect(screen.getByTestId("kb-head-new-kb")).toHaveClass("settings-button--secondary");
    expect(screen.getByTestId("index-notice")).toBeInTheDocument();
    const mobile = screen.getByTestId("mobile-actions");
    expect(within(mobile).getByRole("button", { name: "新建知识库" })).toBeInTheDocument();
    expect(within(mobile).getByRole("button", { name: "添加文件" })).toBeInTheDocument();
  });

  it("other tabs keep Add files as a secondary action, without New knowledge base or the index notice", async () => {
    mocks.search = "tab=templates";
    renderIntl(<KnowledgePage />);
    await screen.findByTestId("matrix-tab");
    expect(screen.getByText("管理矩阵里可重复使用的问题列组。")).toBeInTheDocument();
    expect(screen.getByTestId("kb-head-upload")).toHaveClass("settings-button--secondary");
    expect(screen.queryByTestId("kb-head-new-kb")).not.toBeInTheDocument();
    expect(screen.queryByTestId("index-notice")).not.toBeInTheDocument();
  });

  it("Add files aims at the current Space's default knowledge base and announces the upload with a way to open it", async () => {
    mocks.search = "tab=matrix";
    renderIntl(<KnowledgePage />);
    fireEvent.click(await screen.findByTestId("kb-head-upload"));
    // In place, on any tab: no navigation to another tab.
    expect(mocks.push).not.toHaveBeenCalled();
    const upload = screen.getByRole("dialog", { name: "upload-stub" });
    expect(upload).toHaveAttribute("data-scope", "space");
    expect(upload).toHaveAttribute("data-kb", "kb_space");

    fireEvent.click(within(upload).getByRole("button", { name: "upload-it" }));
    await waitFor(() => expect(mocks.toast).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("dialog", { name: "upload-stub" })).not.toBeInTheDocument();
    expect(mocks.notifyChanged).toHaveBeenCalled();
    const [{ kind, text, action }] = mocks.toast.mock.calls[0] as [{ kind: string; text: string; action: { label: string; onAction: () => void } }];
    expect(kind).toBe("success");
    expect(text).toBe("已添加 排班规则.md 到「门店运营手册」，处理完成后即可在对话中使用");
    expect(action.label).toBe("打开知识库");
    action.onAction();
    expect(mocks.push).toHaveBeenCalledWith("/knowledge/base/?id=kb_ops");
  });

  it("Add files falls back to My knowledge without a Space default", async () => {
    const directory = directoryFixture();
    mocks.dir.directory = { ...directory, sections: { ...directory.sections, space: [] } };
    renderIntl(<KnowledgePage />);
    fireEvent.click(await screen.findByTestId("kb-head-upload"));
    const upload = screen.getByRole("dialog", { name: "upload-stub" });
    expect(upload).toHaveAttribute("data-scope", "personal");
    expect(upload).toHaveAttribute("data-kb", "kb_my");
  });

  it("New knowledge base opens the dialog and lands in the created knowledge base", async () => {
    renderIntl(<KnowledgePage />);
    fireEvent.click(await screen.findByTestId("kb-head-new-kb"));
    fireEvent.click(within(screen.getByRole("dialog", { name: "create-stub" })).getByRole("button", { name: "create-it" }));
    expect(mocks.push).toHaveBeenCalledWith("/knowledge/base/?id=kb_new");
    expect(screen.queryByRole("dialog", { name: "create-stub" })).not.toBeInTheDocument();
  });

  it.each([
    ["file_id=f_1", "/knowledge/base/?file_id=f_1"],
    ["scope=space&space=ws_2&file_id=f_1&chunk_id=c_9", "/knowledge/base/?file_id=f_1&chunk_id=c_9&space=ws_2"],
    ["tab=files&scope=team&file_id=f_2", "/knowledge/base/?file_id=f_2"],
  ])("legacy file link ?%s moves to the file's knowledge base page", async (search, target) => {
    mocks.search = search;
    renderIntl(<KnowledgePage />);
    await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith(target));
    expect(mocks.replace).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("kb-directory-stub")).not.toBeInTheDocument();
  });

  it("file_id on a matrix tab is not a legacy file link", async () => {
    mocks.search = "tab=matrix&view=mx_1&file_id=f_1";
    renderIntl(<KnowledgePage />);
    await screen.findByTestId("matrix-tab");
    expect(mocks.replace).not.toHaveBeenCalled();
  });
});

describe("/knowledge · knowledge bases switch off", () => {
  beforeEach(() => {
    mocks.caps = {};
  });

  it("keeps today's tabs, labels, counts, header and Files manager, and reads no directory", async () => {
    renderIntl(<KnowledgePage />);
    const nav = await screen.findByTestId("kb-nav");
    expect(within(nav).getAllByRole("link").map((link) => link.querySelector("span")?.textContent)).toEqual([
      "文件",
      "文档矩阵",
      "提取结果",
      "模板",
    ]);
    await waitFor(() => expect(screen.getByTestId("kb-nav-records")).toHaveTextContent("提取结果2"));
    expect(screen.getByTestId("kb-nav-templates")).toHaveTextContent("模板6");
    expect(screen.getByTestId("files-manager")).toBeInTheDocument();
    expect(screen.getByTestId("index-notice")).toBeInTheDocument();
    expect(screen.getByTestId("kb-head-upload")).toHaveTextContent("添加知识");
    expect(screen.queryByTestId("kb-head-new-kb")).not.toBeInTheDocument();
    expect(screen.queryByTestId("kb-directory-stub")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "资料库 › 文件" })).toBeInTheDocument();
    expect(mocks.dirEnabled.every((enabled) => !enabled)).toBe(true);
  });

  it("keeps file deep links and the header upload in the Files manager", async () => {
    mocks.search = "scope=space&space=ws_1&file_id=f_1&chunk_id=c_1";
    renderIntl(<KnowledgePage />);
    const files = await screen.findByTestId("files-manager");
    expect(files).toHaveAttribute("data-file-id", "f_1");
    expect(mocks.replace).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("kb-head-upload"));
    expect(files).toHaveAttribute("data-upload-request", "1");
    expect(screen.queryByRole("dialog", { name: "upload-stub" })).not.toBeInTheDocument();
  });

  it.each([["tab=records", "records"], ["tab=templates", "templates"], ["tab=bases", "files"]])(
    "?%s keeps today's view without the knowledge base layout",
    async (search, section) => {
      mocks.search = search;
      renderIntl(<KnowledgePage />);
      if (section === "files") {
        expect(await screen.findByTestId("files-manager")).toBeInTheDocument();
        return;
      }
      const tab = await screen.findByTestId("matrix-tab");
      expect(tab).toHaveAttribute("data-section", section);
      expect(tab).toHaveAttribute("data-kb-layout", "false");
    },
  );
});
