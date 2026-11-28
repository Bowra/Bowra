// Knowledge bases (FEATURE_KNOWLEDGE_BASES): New knowledge base (PRD §4.4 ⑤, creation rights §3).
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";
import type { KnowledgeBaseDirectory } from "@/lib/knowledge-bases-api";

import { directoryFixture, kbFixture } from "./kb-directory.fixtures";

const mocks = vi.hoisted(() => ({
  dir: { directory: null as KnowledgeBaseDirectory | null, loading: false, error: null as Error | null, refresh: vi.fn() },
  role: "member" as "admin" | "member" | "viewer",
  create: vi.fn(),
  notifyChanged: vi.fn(),
}));

vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => ({
    items: [{ id: "ws_1", name: "商超零售场景", desc: "", role: mocks.role }],
    activeId: "ws_1",
    loading: false,
    error: null,
    refresh: vi.fn(),
  }),
}));
vi.mock("./use-knowledge-bases", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./use-knowledge-bases")>();
  return { ...actual, useKnowledgeBaseDirectory: () => mocks.dir };
});
vi.mock("@/lib/knowledge-bases-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/knowledge-bases-api")>();
  return {
    ...actual,
    createKnowledgeBase: (...args: unknown[]) => mocks.create(...args),
    notifyKnowledgeBasesChanged: () => mocks.notifyChanged(),
  };
});

import { KbCreateDialog } from "./kb-create-dialog";

beforeEach(() => {
  mocks.dir = { directory: directoryFixture(), loading: false, error: null, refresh: vi.fn() };
  mocks.role = "member";
  mocks.create.mockReset();
  mocks.notifyChanged.mockReset();
});
afterEach(cleanup);

function homeCard(home: "personal" | "space" | "team") {
  return screen.getByTestId(`kb-create-home-${home}`);
}

function renderDialog(onCreated = vi.fn(), onClose = vi.fn()) {
  renderIntl(<KbCreateDialog onClose={onClose} onCreated={onCreated} />);
  return { onCreated, onClose };
}

describe("KbCreateDialog · where it lives", () => {
  it("offers My, the current Space by name and the Team; options you cannot pick stay visible, disabled, with the reason", () => {
    renderDialog();
    const group = screen.getByRole("radiogroup", { name: "放在" });
    expect(within(group).getAllByRole("radio")).toHaveLength(3);
    expect(homeCard("personal")).toHaveTextContent("只有你能使用，在你所有的空间里都能用");
    expect(homeCard("space")).toHaveTextContent("空间「商超零售场景」");
    // The current Space is preselected when allowed.
    expect(homeCard("space")).toHaveAttribute("aria-checked", "true");
    expect(homeCard("team")).toBeDisabled();
    expect(homeCard("team")).toHaveTextContent("只有团队负责人和团队管理员可以在团队里新建知识库。");
  });

  it("explains the Space option to a Space viewer and falls back to My", () => {
    mocks.role = "viewer";
    mocks.dir.directory = directoryFixture({ can_create: { personal: true, space: false, team: false } });
    renderDialog();
    expect(homeCard("space")).toBeDisabled();
    expect(homeCard("space")).toHaveTextContent("你是这个空间的只读成员，不能在这里新建知识库。需要时请联系空间管理员。");
    expect(homeCard("personal")).toHaveAttribute("aria-checked", "true");
  });

  it("explains the Space option without a current Space", () => {
    mocks.dir.directory = directoryFixture({ space_id: null, can_create: { personal: true, space: false, team: true } });
    renderDialog();
    expect(homeCard("space")).toHaveTextContent("现在没有选中的空间。请先在顶部选择一个空间。");
    expect(homeCard("team")).not.toBeDisabled();
  });

  it("waits for the creation rights before offering homes", () => {
    mocks.dir = { directory: null, loading: true, error: null, refresh: vi.fn() };
    renderDialog();
    expect(screen.getByRole("status", { name: "正在读取你可以新建知识库的位置…" })).toBeInTheDocument();
    expect(screen.getByTestId("kb-create-submit")).toBeDisabled();
  });
});

describe("KbCreateDialog · who can use it", () => {
  it("My is always only me; Space offers members only or all Space members with a default level; Team offers members only or everyone", () => {
    mocks.dir.directory = directoryFixture({ can_create: { personal: true, space: true, team: true } });
    renderDialog();
    // Space (preselected): members only by default, with the hint.
    const audience = () => screen.getByTestId("kb-create-audience");
    expect(within(audience()).getByRole("radio", { name: "仅成员" })).toHaveAttribute("aria-checked", "true");
    expect(audience()).toHaveTextContent("创建后可以在「成员」里添加空间成员。");
    fireEvent.click(within(audience()).getByRole("radio", { name: "空间所有成员" }));
    // Default level: can use (Q3), or can edit.
    expect(screen.getByTestId("kb-create-level-use")).toHaveAttribute("aria-checked", "true");
    expect(audience()).toHaveTextContent("这个空间的所有成员都能在对话中使用");
    fireEvent.click(screen.getByTestId("kb-create-level-edit"));
    expect(audience()).toHaveTextContent("这个空间的所有成员都能使用，也能添加文件。");

    fireEvent.click(homeCard("team"));
    expect(within(audience()).getByRole("radio", { name: "团队所有人可使用" })).toBeInTheDocument();
    expect(screen.queryByTestId("kb-create-level-use")).not.toBeInTheDocument();

    fireEvent.click(homeCard("personal"));
    expect(screen.getByTestId("kb-create-audience-fixed")).toHaveTextContent("仅自己。我的知识库不能分享");
    expect(within(audience()).queryByRole("radio")).not.toBeInTheDocument();
  });
});

describe("KbCreateDialog · create", () => {
  it("requires a name and checks the length limits before sending", () => {
    renderDialog();
    fireEvent.click(screen.getByTestId("kb-create-submit"));
    expect(screen.getByText("请填写名称。")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "长".repeat(121) } });
    expect(screen.getByText("名称最多 120 个字，现在是 121 个。")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("kb-create-submit"));
    expect(mocks.create).not.toHaveBeenCalled();
  });

  it("sends the trimmed name, description, home and audience, then refreshes readers and hands back the knowledge base", async () => {
    const created = kbFixture({ id: "kb_new", name: "供应商合同" });
    mocks.create.mockResolvedValue(created);
    const { onCreated } = renderDialog();
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "  供应商合同 " } });
    fireEvent.change(screen.getByLabelText("说明"), { target: { value: "采购合同与补充协议" } });
    fireEvent.click(within(screen.getByTestId("kb-create-audience")).getByRole("radio", { name: "空间所有成员" }));
    fireEvent.click(screen.getByTestId("kb-create-level-edit"));
    fireEvent.click(screen.getByTestId("kb-create-submit"));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(created));
    expect(mocks.create).toHaveBeenCalledWith({
      name: "供应商合同",
      description: "采购合同与补充协议",
      home: "space",
      audience: "space",
      audience_level: "edit",
    });
    expect(mocks.notifyChanged).toHaveBeenCalledTimes(1);
  });

  it("creates My knowledge bases with the only-me audience and no level", async () => {
    mocks.create.mockResolvedValue(kbFixture({ id: "kb_mine", home: "personal" }));
    renderDialog();
    fireEvent.click(homeCard("personal"));
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "竞品资料" } });
    fireEvent.click(screen.getByTestId("kb-create-submit"));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledWith({ name: "竞品资料", description: null, home: "personal", audience: "owner" }));
  });

  it("explains a refusal with the server's finite code and reads the rights again", async () => {
    mocks.create.mockRejectedValue(new ApiError(403, "Space viewers can't…", { code: "knowledge_base_create_forbidden" }));
    const { onCreated } = renderDialog();
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "门店运营" } });
    fireEvent.click(screen.getByTestId("kb-create-submit"));
    expect(await screen.findByText(/你不能在这里新建知识库：空间只读成员不能新建空间知识库/)).toBeInTheDocument();
    expect(screen.queryByText("Space viewers can't…")).not.toBeInTheDocument();
    expect(mocks.dir.refresh).toHaveBeenCalled();
    expect(onCreated).not.toHaveBeenCalled();
    expect(screen.getByTestId("kb-create-submit")).not.toBeDisabled();
  });
});
